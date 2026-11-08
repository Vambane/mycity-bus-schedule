"""Deterministic timetable export from the committed Parquet snapshot.

The static site never opens DuckDB in the browser. This module is the only
place that reads ``data/snapshot/*.parquet`` for Cloudflare Pages, and it
does not scrape, call EskomSePush, or write ``data/myciti.duckdb``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path

import duckdb

from fastapi_app.services.service_day import EXTRA_HOLIDAYS
from fastapi_app.services.stop_locations import stop_locations
from system_map import build_network, get_route_colors

REPO = Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = REPO / "data" / "snapshot"
DAY_TYPES = ("weekday", "saturday", "sunday")
SOURCE_SUFFIXES = {".py", ".mjs", ".js", ".html", ".css"}


def snapshot_token() -> str:
    """Short hash of the snapshot plus the static-site sources.

    Asset URLs use this so a data or planner change is not served from an
    old immutable cache. The hash does not include build time.
    """
    digest = hashlib.sha256()
    for path in sorted(SNAPSHOT_DIR.glob("*.parquet")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    pages = REPO / "pages"
    for path in sorted(pages.rglob("*")):
        if path.is_file() and path.suffix in SOURCE_SUFFIXES and "dist" not in path.parts:
            digest.update(str(path.relative_to(pages)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def open_snapshot() -> duckdb.DuckDBPyConnection:
    """Load the committed snapshot into an in-memory DuckDB connection."""
    snapshots = sorted(SNAPSHOT_DIR.glob("*.parquet"))
    if not snapshots:
        raise FileNotFoundError(f"No Parquet snapshot in {SNAPSHOT_DIR}")
    con = duckdb.connect()
    for path in snapshots:
        con.execute(
            f"CREATE TABLE {path.stem} AS SELECT * FROM read_parquet(?)",
            [str(path)],
        )
    return con


def _hms(value: object) -> str:
    """Zero-pad a departure to HH:MM:SS. Hours may be 24 or more."""
    raw = str(value)
    parts = raw.split(":")
    if len(parts) < 2:
        return raw
    seconds = int(float(parts[2])) if len(parts) > 2 else 0
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{seconds:02d}"


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _display(value: datetime | None) -> str:
    if value is None:
        return "unknown — run ETL to refresh"
    return value.strftime("%d %b %Y, %H:%M") + " Africa/Johannesburg"


def export_day(con: duckdb.DuckDBPyConnection, day_type: str) -> dict:
    """One service day as ordered lines. Stop order is first departure, then name."""
    rows = con.execute(
        """
        SELECT   route_id,
                 direction,
                 stop_name,
                 MIN(departure_time) AS first_dep,
                 LIST(departure_time ORDER BY departure_time) AS times,
                 ANY_VALUE(operator) AS operator
        FROM     departures
        WHERE    day_type = ?
        GROUP BY route_id, direction, stop_name
        ORDER BY route_id, direction, first_dep, stop_name
        """,
        [day_type],
    ).fetchall()
    lines: dict[tuple[str, str], dict] = {}
    for route_id, direction, stop_name, _first, times, operator in rows:
        key = (route_id, direction)
        line = lines.get(key)
        if line is None:
            line = {"r": route_id, "d": direction, "o": operator, "s": [], "t": []}
            lines[key] = line
        line["s"].append(stop_name)
        line["t"].append([_hms(item) for item in list(times)])
    ordered = [lines[key] for key in sorted(lines)]
    return {"day_type": day_type, "lines": ordered}


def _route_meta(con: duckdb.DuckDBPyConnection) -> dict:
    colors = get_route_colors(con)
    rows = con.execute(
        "SELECT route_id, route_name, operator FROM routes ORDER BY route_id"
    ).fetchall()
    return {
        route_id: {"name": name, "operator": operator, "color": colors.get(route_id, "#666666")}
        for route_id, name, operator in rows
    }


def _scraped(con: duckdb.DuckDBPyConnection) -> tuple[datetime | None, dict[str, str]]:
    latest = con.execute(
        "SELECT MAX(finished_at) FROM scrape_log WHERE status = 'success'"
    ).fetchone()[0]
    per = con.execute(
        """
        SELECT operator, MAX(finished_at)
        FROM scrape_log
        WHERE status = 'success'
        GROUP BY operator
        ORDER BY operator
        """
    ).fetchall()
    return latest, {op: _display(stamp) for op, stamp in per}


def export_meta(con: duckdb.DuckDBPyConnection, token: str) -> dict:
    """Small catalog: routes, validity, scrape dates. No API keys."""
    latest, per_operator = _scraped(con)
    uct_rows = con.execute(
        """
        SELECT route_id, MIN(valid_from), MAX(valid_until)
        FROM timetables
        WHERE operator = 'uct'
        GROUP BY route_id
        ORDER BY route_id
        """
    ).fetchall()
    rail_rows = con.execute(
        """
        SELECT route_name, valid_from
        FROM timetables
        WHERE operator = 'metrorail' AND day_type = 'saturday'
        ORDER BY route_name
        """
    ).fetchall()
    rail_stops = [
        row[0] for row in con.execute(
            "SELECT DISTINCT stop_name FROM stops WHERE operator = 'metrorail' ORDER BY stop_name"
        ).fetchall()
    ]
    counts = {
        "departures": con.execute("SELECT COUNT(*) FROM departures").fetchone()[0],
        "routes": con.execute("SELECT COUNT(*) FROM routes").fetchone()[0],
    }
    return {
        "version": "2.0.0-pages",
        "timezone": "Africa/Johannesburg",
        "snapshot": token,
        "scraped_at": _iso(latest),
        "scraped_at_display": _display(latest),
        "scraped_at_by_operator": per_operator,
        "routes": _route_meta(con),
        "uct_validity": {
            route_id: {"from": _iso(start), "until": _iso(end)}
            for route_id, start, end in uct_rows
        },
        "rail_saturday": [
            {"name": name, "valid_from": _iso(valid_from)}
            for name, valid_from in rail_rows
        ],
        "rail_stops": rail_stops,
        "extra_holidays": [
            {"date": day.isoformat(), "name": name}
            for day, name in sorted(EXTRA_HOLIDAYS.items())
        ],
        "counts": counts,
        "load_shedding": {
            "available": False,
            "reason": (
                "Static mode does not call EskomSePush. Set data/ls.json endpoint "
                "to the optional Worker. Stop block polygons are not in the snapshot, "
                "so per-trip outage flags stay off."
            ),
        },
    }


def _names(con: duckdb.DuckDBPyConnection, operator: str) -> list[str]:
    if operator == "both":
        rows = con.execute("SELECT DISTINCT stop_name FROM stops ORDER BY stop_name").fetchall()
    else:
        rows = con.execute(
            "SELECT DISTINCT stop_name FROM stops WHERE operator = ? ORDER BY stop_name",
            [operator],
        ).fetchall()
    return [row[0] for row in rows]


def export_stops(con: duckdb.DuckDBPyConnection) -> dict:
    """Autocomplete names plus the same coordinates the server locates."""
    operators = ("both", "myciti", "uct", "metrorail")
    names = {op: _names(con, op) for op in operators}
    located = {}
    unmapped = {}
    for op in operators:
        payload = stop_locations(con, op)
        located[op] = payload["stops"]
        unmapped[op] = payload["unmapped"]
    return {"names": names, "locations": located, "unmapped": unmapped}


def export_network(con: duckdb.DuckDBPyConnection) -> dict:
    """System map graph, with stable ordering so rebuilds match byte for byte."""
    graph = build_network(con, "both")
    graph["nodes"] = sorted(graph["nodes"], key=lambda node: node["id"])
    graph["links"] = sorted(
        graph["links"], key=lambda link: (link["route"], link["source"], link["target"])
    )
    graph["routes"] = sorted(graph["routes"], key=lambda route: route["id"])
    graph["paths"] = {key: graph["paths"][key] for key in sorted(graph["paths"])}
    return graph


def dump_json(path: Path, payload: object) -> None:
    """Compact UTF-8 JSON with a trailing newline. Key order is the object's."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    path.write_text(text + "\n", encoding="utf-8")
