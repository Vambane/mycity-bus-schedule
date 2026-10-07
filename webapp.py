"""Flask API and static website for the MyCiTi timetable."""

from functools import lru_cache
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
from flask import Flask, jsonify, request, send_from_directory

from journey import find_connections, find_transfer_connections
from system_map import build_network


ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "myciti.duckdb"
SNAPSHOT_DIR = DATA_DIR / "snapshot"
LOCAL_TZ = ZoneInfo("Africa/Johannesburg")
DAY_TYPES = {"weekday", "saturday", "sunday"}
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="website", static_url_path="/assets")


def _ensure_database() -> None:
    """Rebuild the local DuckDB file from the committed snapshot if needed."""
    if DB_PATH.exists():
        return
    snapshots = sorted(SNAPSHOT_DIR.glob("*.parquet"))
    if not snapshots:
        raise FileNotFoundError(f"No timetable snapshot found in {SNAPSHOT_DIR}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    try:
        for parquet in snapshots:
            con.execute(
                f"CREATE TABLE IF NOT EXISTS {parquet.stem} "
                "AS SELECT * FROM read_parquet(?)",
                [str(parquet)],
            )
    finally:
        con.close()


@lru_cache(maxsize=1)
def _network() -> dict:
    """Build the stop and route map once per server process."""
    _ensure_database()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        return build_network(con)
    finally:
        con.close()


def _rows(frame) -> list[dict]:
    """Convert pandas/numpy values into plain JSON-compatible objects."""
    return json.loads(frame.to_json(orient="records"))


def _day_type(value: str | None) -> str:
    value = value or "weekday"
    if value not in DAY_TYPES:
        raise ValueError("Day must be weekday, saturday, or sunday.")
    return value


def _stop(value: str | None) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("Choose a stop to continue.")
    _ensure_database()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        found = con.execute(
            "SELECT 1 FROM departures WHERE stop_name = ? LIMIT 1", [value]
        ).fetchone()
    finally:
        con.close()
    if not found:
        raise ValueError("That stop is not in the timetable.")
    return value


@app.errorhandler(ValueError)
def _bad_request(error):
    return jsonify(error=str(error)), 400


@app.get("/")
def home():
    """Serve the website shell."""
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    """Expose a lightweight health check for the hosting platform."""
    return jsonify(status="ok")


@app.get("/api/bootstrap")
def bootstrap():
    """Return stop options, route geometry, and timetable metadata."""
    _ensure_database()
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        stops = [
            row[0] for row in con.execute(
                "SELECT DISTINCT stop_name FROM departures ORDER BY stop_name"
            ).fetchall()
        ]
        info = con.execute(
            """
            SELECT finished_at, routes_loaded, stops_loaded, departures_loaded
            FROM scrape_log ORDER BY run_id DESC LIMIT 1
            """
        ).fetchone()
    finally:
        con.close()

    return jsonify({
        "stops": stops,
        "network": _network(),
        "localTime": datetime.now(LOCAL_TZ).strftime("%H:%M"),
        "dayType": (
            "weekday" if datetime.now(LOCAL_TZ).weekday() < 5
            else "saturday" if datetime.now(LOCAL_TZ).weekday() == 5
            else "sunday"
        ),
        "updated": str(info[0])[:16] if info else None,
        "counts": {
            "routes": info[1],
            "stops": info[2],
            "departures": info[3],
        } if info else None,
    })


@app.get("/api/journey")
def journey():
    """Find direct or connecting journeys between two stops."""
    origin = _stop(request.args.get("from"))
    destination = _stop(request.args.get("to"))
    day_type = _day_type(request.args.get("day"))
    if origin == destination:
        raise ValueError("Choose two different stops.")

    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        direct = find_connections(con, origin, destination, day_type)
        if not direct.empty:
            results = _rows(direct)
            kind = "direct"
        else:
            transfers = find_transfer_connections(
                con, origin, destination, day_type
            )
            results = _rows(transfers)
            kind = "transfer" if results else "none"
    finally:
        con.close()

    now = datetime.now(LOCAL_TZ).strftime("%H:%M:%S")
    upcoming = [row for row in results if row["dep"] > now]
    return jsonify({
        "kind": kind,
        "results": results,
        "upcoming": upcoming,
        "now": now[:5],
    })


@app.get("/api/stop")
def stop_board():
    """Return upcoming departures for one stop."""
    stop_name = _stop(request.args.get("stop"))
    day_type = _day_type(request.args.get("day"))
    now = datetime.now(LOCAL_TZ).strftime("%H:%M:%S")
    con = duckdb.connect(str(DB_PATH), read_only=True)
    try:
        routes = con.execute(
            """
            SELECT DISTINCT r.route_id, r.route_name
            FROM stops s JOIN routes r ON r.route_id = s.route_id
            WHERE s.stop_name = ? ORDER BY r.route_id
            """,
            [stop_name],
        ).fetchall()
        upcoming = con.execute(
            """
            SELECT DISTINCT d.route_id, r.route_name, d.direction, d.departure_time
            FROM departures d JOIN routes r ON r.route_id = d.route_id
            WHERE d.stop_name = ? AND d.day_type = ? AND d.departure_time > ?
            ORDER BY d.route_id, d.direction, d.departure_time
            """,
            [stop_name, day_type, now],
        ).df()
    finally:
        con.close()

    return jsonify({
        "stop": stop_name,
        "routes": [{"id": row[0], "name": row[1]} for row in routes],
        "departures": _rows(upcoming),
        "now": now[:5],
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
