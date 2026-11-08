"""Honest notes for Metrorail results: no Sunday trains, old Saturday sheets.

Halte runs every operator on its Sunday timetable on public holidays
(services/service_day.py). Metrorail has no Sunday service and a separate
holiday timetable that is not loaded (etl/scrape_metrorail.py), so on a
Sunday or holiday the rider is told why no trains show rather than being
left with an empty result.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable, Optional

import duckdb

# Short line names for route chips ("MR-S -> Northern" reads badly as ids).
RAIL_SHORT_NAMES = {
    "MR-S": "Southern",
    "MR-CF": "Cape Flats",
    "MR-C": "Central",
    "MR-N": "Northern",
    "MR-M": "Malmesbury",
}

PRASA_SCHEDULES_URL = "https://www.prasa.com/train-schedules/"

# Older than this and a Saturday sheet gets a caution.
STALE_AFTER_DAYS = 180


def badge_label(route_id: object) -> str:
    """Chip text for a route id: rail lines by name, buses by their number."""
    rid = str(route_id or "")
    return RAIL_SHORT_NAMES.get(rid, rid)


def _fmt(day: date) -> str:
    return f"{day.day} {day:%B %Y}"


def serves_rail(conn: duckdb.DuckDBPyConnection, stop_names: Iterable[Optional[str]]) -> bool:
    """True when any of the given stops is a Metrorail station."""
    names = [n for n in stop_names if n]
    if not names:
        return False
    placeholders = ", ".join("?" * len(names))
    row = conn.execute(
        f"SELECT 1 FROM stops WHERE operator = 'metrorail' "
        f"AND stop_name IN ({placeholders}) LIMIT 1",
        names,
    ).fetchone()
    return row is not None


def metrorail_notice(
    conn: duckdb.DuckDBPyConnection,
    day_type: str,
    *,
    today: Optional[date] = None,
) -> Optional[dict]:
    """A {"title", "body"} note for Metrorail on *day_type*, or None.

    Callers decide whether rail is in play (Metrorail filter, or a station
    in the search); this only decides what, if anything, to say.
    """
    today = today or date.today()
    if day_type == "sunday":
        return {
            "title": "No Metrorail trains on Sundays",
            "body": (
                "Metrorail doesn't run on Sundays. On public holidays PRASA runs "
                "a separate holiday timetable that Halte doesn't show yet, so "
                "check prasa.com before you travel."
            ),
            "url": PRASA_SCHEDULES_URL,
        }
    if day_type != "saturday":
        return None
    rows = conn.execute(
        "SELECT route_name, valid_from FROM timetables "
        "WHERE operator = 'metrorail' AND day_type = 'saturday' ORDER BY route_name"
    ).fetchall()
    old = [
        (name, vf) for name, vf in rows
        if vf is None or (today - vf).days > STALE_AFTER_DAYS
    ]
    if not old:
        return None
    parts = [f"{name} ({_fmt(vf)})" if vf else f"{name} (undated)" for name, vf in old]
    return {
        "title": "Some Saturday train times are old",
        "body": (
            "These Saturday sheets are the newest Halte has: "
            + ", ".join(parts)
            + ". PRASA may have changed them since, so check before you travel."
        ),
        "url": PRASA_SCHEDULES_URL,
    }
