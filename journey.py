"""
journey.py — Direct and connecting bus journeys between two stops
==================================================================
Powers the flight-search-style "From → To" results in the app.

A direct connection is a single route+direction that serves both stops
with the origin earlier in the stop sequence than the destination. Trips
are not stored as entities, but within one route+direction+day the k-th
departure at every stop belongs to the k-th trip — so pairing the k-th
time at the origin with the k-th time at the destination reconstructs
each trip's departure and arrival. When the two stops have unequal trip
counts (some trips skip a stop), we fall back to the nearest later time.

When no direct service exists, find_transfer_connections searches for
journeys with one transfer (ride line A to a shared stop, change to
line B), falling back to two transfers via a bridging line. Transfers
require a minimum changeover buffer and a bounded wait; results are
timetable-based suggestions, not guaranteed connections.
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


# ---------------------------------------------------------------------------
# Transfer search
# ---------------------------------------------------------------------------

MIN_TRANSFER_MIN = 3    # changeover buffer at a transfer stop
MAX_WAIT_MIN = 45       # longest acceptable wait for the next leg
MAX_LEG_MIN = 300       # per-leg sanity bound (same as direct search)
MAX_TOTAL_MIN = 420     # door-to-door sanity bound for transfer journeys

# Candidate transfer stops tried per line pair, nearest first. Keeps the
# two-transfer search bounded; missing an exotic changeover is acceptable
# because results are suggestions, not an exhaustive router.
_MAX_HOPS_PER_LINE = 4

_TRANSFER_COLUMNS = [
    "dep", "arr", "duration_min", "duration",
    "transfers", "route_ids", "via", "legs",
]


def _load_lines(
    con: duckdb.DuckDBPyConnection, day_type: str,
) -> dict[tuple[str, str], dict[str, tuple[str, list[str]]]]:
    """Group the day's departures into lines.

    Returns {(route_id, direction): {stop_name: (first_dep, times)}} where
    first_dep encodes the stop's position along the line (the first trip
    of the day visits stops in order, as in the direct search).
    """
    df = con.execute(
        """
        SELECT   route_id,
                 direction,
                 stop_name,
                 MIN(departure_time) AS first_dep,
                 LIST(departure_time ORDER BY departure_time) AS times
        FROM     departures
        WHERE    day_type = ?
        GROUP BY route_id, direction, stop_name
        """,
        [day_type],
    ).df()
    lines: dict[tuple[str, str], dict[str, tuple[str, list[str]]]] = {}
    for row in df.itertuples():
        lines.setdefault((row.route_id, row.direction), {})[row.stop_name] = (
            row.first_dep, list(row.times),
        )
    return lines


def _leg_pairs(
    stops: dict[str, tuple[str, list[str]]], s1: str, s2: str,
) -> list[tuple[str, str, int, int]]:
    """Sane (dep, arr, dep_min, arr_min) tuples riding one line s1 → s2."""
    if stops[s1][0] >= stops[s2][0]:  # s2 not downstream of s1
        return []
    pairs = []
    for dep, arr in _pair_times(stops[s1][1], stops[s2][1]):
        minutes = _to_minutes(arr) - _to_minutes(dep)
        if 0 < minutes <= MAX_LEG_MIN:
            pairs.append((dep, arr, _to_minutes(dep), _to_minutes(arr)))
    return pairs


def _next_leg(
    pairs: list[tuple[str, str, int, int]], arrived_min: int,
) -> tuple[str, str, int, int] | None:
    """First onward departure within the transfer window, or None.

    The window is [arrived + MIN_TRANSFER_MIN, arrived + MAX_WAIT_MIN];
    pairs are sorted by departure, so the first at or past the buffer is
    the one a rider would take.
    """
    lo = arrived_min + MIN_TRANSFER_MIN
    hi = arrived_min + MAX_WAIT_MIN
    for pair in pairs:
        if pair[2] >= lo:
            return pair if pair[2] <= hi else None
    return None


def _itinerary_row(legs: list[dict], route_names: dict[str, str]) -> dict:
    """Assemble one result row from boarded legs (adds waits and totals)."""
    for prev, leg in zip(legs, legs[1:]):
        leg["wait_min"] = _to_minutes(leg["dep"]) - _to_minutes(prev["arr"])
    for leg in legs:
        leg.setdefault("wait_min", 0)
        leg["route_name"] = route_names.get(leg["route_id"], leg["route_id"])
    total = _to_minutes(legs[-1]["arr"]) - _to_minutes(legs[0]["dep"])
    return {
        "dep": legs[0]["dep"],
        "arr": legs[-1]["arr"],
        "duration_min": total,
        "duration": _fmt_duration(total),
        "transfers": len(legs) - 1,
        "route_ids": [leg["route_id"] for leg in legs],
        "via": [leg["board"] for leg in legs[1:]],
        "legs": legs,
    }


def _leg(line: tuple[str, str], board: str, alight: str,
         pair: tuple[str, str, int, int]) -> dict:
    """One boarded leg of an itinerary."""
    return {
        "route_id": line[0],
        "direction": line[1],
        "board": board,
        "alight": alight,
        "dep": pair[0],
        "arr": pair[1],
    }


def _one_transfer_rows(
    lines_from: dict, lines_to: dict,
    from_stop: str, to_stop: str, route_names: dict[str, str],
) -> list[dict]:
    """Itineraries riding line A to a shared stop, then line B."""
    rows = []
    for la, sa in lines_from.items():
        for lb, sb in lines_to.items():
            if la[0] == lb[0]:  # changing to the same route is not a journey
                continue
            xs = [
                x for x in sa
                if x in sb and x not in (from_stop, to_stop)
                and sa[x][0] > sa[from_stop][0] and sb[x][0] < sb[to_stop][0]
            ]
            xs.sort(key=lambda x, _sa=sa: _sa[x][0])  # nearest changeover first
            for x in xs[:_MAX_HOPS_PER_LINE]:
                leg2_pairs = _leg_pairs(sb, x, to_stop)
                if not leg2_pairs:
                    continue
                for pair1 in _leg_pairs(sa, from_stop, x):
                    pair2 = _next_leg(leg2_pairs, pair1[3])
                    if pair2 is None or pair2[3] - pair1[2] > MAX_TOTAL_MIN:
                        continue
                    rows.append(_itinerary_row([
                        _leg(la, from_stop, x, pair1),
                        _leg(lb, x, to_stop, pair2),
                    ], route_names))
    return rows


def _two_transfer_rows(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    lines: dict, lines_from: dict, lines_to: dict,
    from_stop: str, to_stop: str, route_names: dict[str, str],
) -> list[dict]:
    """Itineraries with a bridging line between origin and destination lines."""
    rows = []
    for la, sa in lines_from.items():
        xs1_all = [x for x in sa if x != from_stop and sa[x][0] > sa[from_stop][0]]
        for lm, sm in lines.items():
            if lm[0] == la[0]:
                continue
            xs1 = [x for x in xs1_all if x in sm and x != to_stop]
            xs1.sort(key=lambda x, _sa=sa: _sa[x][0])
            xs1 = xs1[:_MAX_HOPS_PER_LINE]
            if not xs1:
                continue
            for lb, sb in lines_to.items():
                if lb[0] in (la[0], lm[0]):
                    continue
                xs2_all = [
                    x for x in sb
                    if x in sm and x not in (from_stop, to_stop)
                    and sb[x][0] < sb[to_stop][0]
                ]
                if not xs2_all:
                    continue
                for x1 in xs1:
                    xs2 = [x for x in xs2_all if sm[x][0] > sm[x1][0]]
                    xs2.sort(key=lambda x, _sm=sm: _sm[x][0])
                    for x2 in xs2[:_MAX_HOPS_PER_LINE]:
                        rows.extend(_three_leg_rows(
                            (la, sa), (lm, sm), (lb, sb),
                            (from_stop, x1, x2, to_stop), route_names,
                        ))
    return rows


def _three_leg_rows(
    line_a: tuple, line_m: tuple, line_b: tuple,
    stops: tuple[str, str, str, str], route_names: dict[str, str],
) -> list[dict]:
    """Time all three legs of one candidate two-transfer routing."""
    (la, sa), (lm, sm), (lb, sb) = line_a, line_m, line_b
    from_stop, x1, x2, to_stop = stops
    legm_pairs = _leg_pairs(sm, x1, x2)
    legb_pairs = _leg_pairs(sb, x2, to_stop)
    if not legm_pairs or not legb_pairs:
        return []
    rows = []
    for pair1 in _leg_pairs(sa, from_stop, x1):
        pair2 = _next_leg(legm_pairs, pair1[3])
        if pair2 is None:
            continue
        pair3 = _next_leg(legb_pairs, pair2[3])
        if pair3 is None or pair3[3] - pair1[2] > MAX_TOTAL_MIN:
            continue
        rows.append(_itinerary_row([
            _leg(la, from_stop, x1, pair1),
            _leg(lm, x1, x2, pair2),
            _leg(lb, x2, to_stop, pair3),
        ], route_names))
    return rows


def _pareto(rows: list[dict]) -> list[dict]:
    """Keep only non-dominated itineraries (leave later AND arrive earlier).

    Per departure time the earliest arrival (fewest transfers on ties)
    wins; then, scanning departures latest-first, an itinerary survives
    only if it arrives earlier than every later-departing survivor.
    Zero-padded GTFS time strings compare chronologically.
    """
    best_by_dep: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: (r["dep"], r["arr"], r["transfers"])):
        best_by_dep.setdefault(row["dep"], row)

    kept: list[dict] = []
    best_arr: str | None = None
    for dep in sorted(best_by_dep, reverse=True):
        row = best_by_dep[dep]
        if best_arr is None or row["arr"] < best_arr:
            kept.append(row)
            best_arr = row["arr"]
    kept.reverse()
    return kept


def find_transfer_connections(
    con: duckdb.DuckDBPyConnection,
    from_stop: str,
    to_stop: str,
    day_type: str,
    max_transfers: int = 2,
) -> pd.DataFrame:
    """
    Return journeys from `from_stop` to `to_stop` requiring transfers.

    One-transfer journeys are searched first; two-transfer journeys (via a
    bridging line) only when no one-transfer option exists. Each transfer
    needs at least MIN_TRANSFER_MIN minutes and at most MAX_WAIT_MIN.

    Returns a DataFrame with [dep, arr, duration_min, duration, transfers,
    route_ids, via, legs] sorted by departure, reduced to the Pareto set
    (no journey that both leaves earlier and arrives later than another).
    Empty if nothing is found within the bounds.
    """
    lines = _load_lines(con, day_type)
    route_names = dict(
        con.execute("SELECT route_id, route_name FROM routes").fetchall()
    )
    lines_from = {k: v for k, v in lines.items() if from_stop in v}
    lines_to = {k: v for k, v in lines.items() if to_stop in v}
    if not lines_from or not lines_to or from_stop == to_stop:
        return pd.DataFrame(columns=_TRANSFER_COLUMNS)

    rows = _one_transfer_rows(
        lines_from, lines_to, from_stop, to_stop, route_names)
    if not rows and max_transfers >= 2:
        rows = _two_transfer_rows(
            lines, lines_from, lines_to, from_stop, to_stop, route_names)

    rows = _pareto(rows)
    if not rows:
        return pd.DataFrame(columns=_TRANSFER_COLUMNS)
    return pd.DataFrame(rows).reset_index(drop=True)
