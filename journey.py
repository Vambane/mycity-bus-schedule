"""
journey.py — Direct bus connections between two stops
======================================================
Powers the flight-search-style "From → To" results in the app.

A connection is a single route+direction that serves both stops with the
origin earlier in the stop sequence than the destination. Trips are not
stored as entities, but within one route+direction+day the k-th departure
at every stop belongs to the k-th trip — so pairing the k-th time at the
origin with the k-th time at the destination reconstructs each trip's
departure and arrival. When the two stops have unequal trip counts (some
trips skip a stop), we fall back to the nearest later time.
"""

import duckdb
import pandas as pd


def _to_minutes(hms: str) -> int:
    """'HH:MM:SS' → minutes since midnight (supports GTFS hours ≥ 24)."""
    h, m, _ = hms.split(":")
    return int(h) * 60 + int(m)


def _fmt_duration(minutes: int) -> str:
    """65 → '1 h 05', 42 → '42 min'."""
    if minutes >= 60:
        return f"{minutes // 60} h {minutes % 60:02d}"
    return f"{minutes} min"


def _pair_times(from_times: list[str], to_times: list[str]) -> list[tuple[str, str]]:
    """
    Pair each origin departure with its trip's destination arrival.

    Equal-length lists pair by index (same trip). Otherwise, each origin
    time pairs with the nearest strictly-later destination time.
    """
    if len(from_times) == len(to_times):
        return list(zip(from_times, to_times))
    pairs = []
    for dep in from_times:
        later = [t for t in to_times if t > dep]
        if later:
            pairs.append((dep, min(later)))
    return pairs


def find_connections(
    con: duckdb.DuckDBPyConnection,
    from_stop: str,
    to_stop: str,
    day_type: str,
) -> pd.DataFrame:
    """
    Return every direct service from `from_stop` to `to_stop` on `day_type`.

    Returns a DataFrame with [route_id, route_name, direction, dep, arr,
    duration_min, duration] sorted by departure time. Empty if no route
    serves both stops in that order.
    """
    # Candidate route+directions: both stops served, origin first. Stop
    # order within a direction is recovered from the first trip of the day
    # (MIN departure), the same technique the system map uses.
    candidates = con.execute(
        """
        WITH ordered AS (
            SELECT   route_id,
                     direction,
                     stop_name,
                     MIN(departure_time) AS first_dep,
                     LIST(departure_time ORDER BY departure_time) AS times
            FROM     departures
            WHERE    day_type = ?
            GROUP BY route_id, direction, stop_name
        )
        SELECT  a.route_id,
                r.route_name,
                a.direction,
                a.times AS from_times,
                b.times AS to_times
        FROM    ordered a
        JOIN    ordered b USING (route_id, direction)
        JOIN    routes  r ON r.route_id = a.route_id
        WHERE   a.stop_name = ?
          AND   b.stop_name = ?
          AND   a.first_dep < b.first_dep
        """,
        [day_type, from_stop, to_stop],
    ).df()

    rows = []
    for _, cand in candidates.iterrows():
        for dep, arr in _pair_times(list(cand["from_times"]), list(cand["to_times"])):
            minutes = _to_minutes(arr) - _to_minutes(dep)
            # Guard against pathological pairings (skipped-stop misalignment)
            if 0 < minutes <= 300:
                rows.append({
                    "route_id": cand["route_id"],
                    "route_name": cand["route_name"],
                    "direction": cand["direction"],
                    "dep": dep,
                    "arr": arr,
                    "duration_min": minutes,
                    "duration": _fmt_duration(minutes),
                })

    if not rows:
        return pd.DataFrame(columns=[
            "route_id", "route_name", "direction",
            "dep", "arr", "duration_min", "duration",
        ])
    return pd.DataFrame(rows).sort_values("dep").reset_index(drop=True)
