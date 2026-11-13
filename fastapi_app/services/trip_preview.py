"""Trip preview (ghost bus) — timetable interpolation, deliberate trigger only.

Design: /workspace/mycity-team/design/trip-preview-interaction.md
Compliance caption (exact): Estimated from timetable — not live GPS.

Where the ghost is drawn
------------------------
The ghost follows the trip from the first stop of its route (when the
timetable lets us follow it that far back), not just from the rider's
boarding stop, so before the bus reaches the rider it shows the bus on its
way. Each stop is pinned to its own point on the street path, and between
two stops the ghost moves by time *and* distance: at the halfway minute
between Woodstock and Paarden Eiland it is halfway along the road between
them. The browser repeats the same calculation every few seconds, so the
ghost keeps moving while the map is open.

The control is only offered for buses due at the rider's stop within
PREVIEW_AHEAD_MIN minutes; a bus three hours away has no position yet.
"""
from __future__ import annotations

import logging
import math
import zoneinfo
from datetime import datetime, timedelta
from typing import Any, Optional

import duckdb

from fastapi_app.config import settings
from fastapi_app.services.service_day import day_type_for
from fastapi_app.services.trust import get_scraped_at_raw
from journey import clock_time
from journey_map import (
    _load_route_paths,
    _load_stop_coords,
    _match_coords,
    _street_slice,
    leg_stop_sequence,
)
from system_map import _station_coords, get_route_colors

logger = logging.getLogger(__name__)

COMPLIANCE_CAPTION = "Estimated from timetable — not live GPS."
OPTIONAL_CAPTION = (
    "Planning aid only. Confirm on MyCiTi / UCT before you travel."
)
OUTSIDE_WINDOW_CAPTION = "Outside trip window — showing scheduled start/end"

# Offer the preview only for buses due at the rider's stop within this many
# minutes. Further ahead there is no meaningful position to show.
PREVIEW_AHEAD_MIN = 30

# Soft staleness: hide preview when scrape is older than this (calendar days).
# Generous so a labelled scrape still previews; missing scrape always hides.
_STALE_MAX_DAYS = 45


def _tz() -> zoneinfo.ZoneInfo:
    return zoneinfo.ZoneInfo(settings.timezone)


def _now_ct() -> datetime:
    return datetime.now(_tz())


def _day_type_for(dt: datetime) -> str:
    """Timetable running at *dt*, public holidays included (service_day.py)."""
    return day_type_for(dt.date())


def _norm_time(value: str) -> str:
    """Normalize HH:MM or HH:MM:SS to HH:MM:SS."""
    value = (value or "").strip()
    if len(value) == 5 and value[2] == ":":
        return value + ":00"
    return value


def _to_minutes(hms: str) -> int:
    parts = _norm_time(hms).split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _mute_color(hex_color: str) -> str:
    """Blend route colour toward mid-grey for a soft ghost look."""
    raw = (hex_color or "#666666").lstrip("#")
    if len(raw) != 6:
        raw = "666666"
    r, g, b = int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    # 55% toward grey 128
    r = int(r * 0.45 + 128 * 0.55)
    g = int(g * 0.45 + 128 * 0.55)
    b = int(b * 0.45 + 128 * 0.55)
    return f"#{r:02x}{g:02x}{b:02x}"


def _path_length(path: list[list[float]]) -> float:
    total = 0.0
    for i in range(1, len(path)):
        a, b = path[i - 1], path[i]
        k = math.cos(math.radians(a[0]))
        total += math.hypot(b[0] - a[0], (b[1] - a[1]) * k)
    return total


def _point_along(path: list[list[float]], fraction: float) -> list[float]:
    """Interpolate a [lat, lon] along path by fraction in [0, 1]."""
    if not path:
        return [0.0, 0.0]
    if len(path) == 1 or fraction <= 0:
        return list(path[0])
    if fraction >= 1:
        return list(path[-1])
    target = _path_length(path) * fraction
    walked = 0.0
    for i in range(1, len(path)):
        a, b = path[i - 1], path[i]
        k = math.cos(math.radians(a[0]))
        seg = math.hypot(b[0] - a[0], (b[1] - a[1]) * k)
        if walked + seg >= target and seg > 0:
            t = (target - walked) / seg
            return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
        walked += seg
    return list(path[-1])


def _memo(memo: Optional[dict], key: tuple, compute):
    """Return memo[key], computing it once. memo=None disables caching.

    One search builds up to ~150 result cards that share a handful of
    route/direction pairs, and each card used to repeat the same queries.
    The database is read-only while the app runs, so within one request
    the answers cannot change.
    """
    if memo is None:
        return compute()
    if key not in memo:
        memo[key] = compute()
    return memo[key]


def cached_leg_stop_sequence(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    memo: Optional[dict],
    con: duckdb.DuckDBPyConnection,
    route_id: str,
    direction: str,
    board: str,
    alight: str,
    day_type: str,
) -> list[str]:
    """leg_stop_sequence(), remembered for the rest of the request."""
    return _memo(
        memo, ("leg", route_id, direction, board, alight, day_type),
        lambda: leg_stop_sequence(con, route_id, direction, board, alight, day_type),
    )


def _stop_times_lists(
    con: duckdb.DuckDBPyConnection,
    route_id: str,
    direction: str,
    day_type: str,
    stops: list[str],
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for stop in stops:
        rows = con.execute(
            """
            SELECT departure_time
            FROM   departures
            WHERE  route_id = ? AND direction = ? AND day_type = ?
                   AND stop_name = ?
            ORDER BY departure_time
            """,
            [route_id, direction, day_type, stop],
        ).fetchall()
        out[stop] = [str(r[0]) for r in rows]
    return out


def _find_trip_index(board_times: list[str], dep: str) -> int | None:
    dep_n = _norm_time(dep)
    dep_hm = dep_n[:5]
    for i, t in enumerate(board_times):
        tn = _norm_time(str(t))
        if tn == dep_n or tn[:5] == dep_hm:
            return i
    return None


# Keyword-only trip description; each early return is one hide reason.
# pylint: disable-next=too-many-arguments,too-many-return-statements
def eligibility(
    *,
    con: duckdb.DuckDBPyConnection,
    memo: Optional[dict] = None,
    day_type: str,
    board: str,
    alight: str,
    route_id: str,
    direction: str,
    dep: str,
    arr: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Return (ok, hide_reason). Reasons are internal; button is hidden when not ok."""
    scraped = _memo(memo, ("scraped",), lambda: get_scraped_at_raw(con))
    if scraped is None:
        return False, "stale"
    now = _now_ct()
    scraped_aware = scraped if scraped.tzinfo else scraped.replace(tzinfo=_tz())
    if now - scraped_aware > timedelta(days=_STALE_MAX_DAYS):
        return False, "stale"

    if day_type != _day_type_for(now):
        return False, "wrong_day"

    seq = cached_leg_stop_sequence(memo, con, route_id, direction, board, alight, day_type)
    if len(seq) < 2:
        return False, "no_geometry"

    times = _memo(
        memo, ("times", route_id, direction, day_type, tuple(seq)),
        lambda: _stop_times_lists(con, route_id, direction, day_type, seq),
    )
    board_times = times.get(board) or []
    if not board_times:
        return False, "no_geometry"
    # Soft pairing: unequal trip counts along the leg
    n = len(board_times)
    if any(len(times.get(s) or []) != n for s in seq):
        return False, "soft"

    idx = _find_trip_index(board_times, dep)
    if idx is None:
        return False, "no_geometry"

    now_min = now.hour * 60 + now.minute
    if _to_minutes(str(board_times[idx])) - now_min > PREVIEW_AHEAD_MIN:
        return False, "too_early"

    trip_arr = arr
    if not trip_arr:
        alight_times = times.get(alight) or []
        trip_arr = alight_times[idx] if idx < len(alight_times) else None
    if trip_arr:
        if now_min > _to_minutes(str(trip_arr)):
            return False, "past_last_bus"

    return True, None


# Keyword-only trip description; one pass from timetable to ghost marker.
# pylint: disable-next=too-many-arguments,too-many-locals,too-many-statements
def build_trip_preview(
    con: duckdb.DuckDBPyConnection,
    *,
    route_id: str,
    direction: str,
    day_type: str,
    board: str,
    alight: str,
    dep: str,
    arr: Optional[str] = None,
) -> dict[str, Any]:
    """Payload for ghost interpolation on the system map.

    Geometry: street polyline when available, else schematic stop-to-stop.
    Clock: Africa/Johannesburg.
    """
    ok, reason = eligibility(
        con=con,
        day_type=day_type,
        board=board,
        alight=alight,
        route_id=route_id,
        direction=direction,
        dep=dep,
        arr=arr,
    )
    base = {
        "eligible": ok,
        "hide_reason": reason,
        "caption": COMPLIANCE_CAPTION,
        "caption_optional": OPTIONAL_CAPTION,
        "clock_zone": settings.timezone,
    }
    if not ok:
        return base

    trip = _trip_stops(con, route_id, direction, day_type, board, alight, dep)
    if trip is None:
        return {**base, "eligible": False, "hide_reason": "no_geometry"}
    timed_stops, board_index = trip

    coords = _match_coords([ts["name"] for ts in timed_stops], _load_stop_coords())
    stations = _station_coords()
    for ts in timed_stops:
        point = stations.get(ts["name"]) or coords.get(ts["name"])
        if point:
            ts["lat"] = round(point[0], 5)
            ts["lon"] = round(point[1], 5)

    located = [ts for ts in timed_stops if "lat" in ts]
    if len(located) < 2:
        return {**base, "eligible": False, "hide_reason": "no_geometry"}
    schematic_pts = [[ts["lat"], ts["lon"]] for ts in located]

    street = _street_slice(
        _load_route_paths().get(route_id.upper(), []),
        schematic_pts[0],
        schematic_pts[-1],
    )
    geometry_source = "street" if street is not None else "schematic"
    path = street if street is not None else schematic_pts
    _pin_stops_to_path(located, path, snap=street is not None)

    colors = get_route_colors(con)
    route_color = colors.get(route_id, "#666666")
    ghost_color = _mute_color(route_color)

    now = _now_ct()
    now_min = now.hour * 60 + now.minute + now.second / 60
    position = ghost_position(located, now_min)
    ghost = _point_at_distance(path, position["d"])
    rider_from = next(ts for ts in timed_stops if ts["role"] == "board")
    outside = position["state"] in ("not_started", "arrived")

    captions = [COMPLIANCE_CAPTION, OPTIONAL_CAPTION]
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    return {
        **base,
        "eligible": True,
        "hide_reason": None,
        "route_id": route_id,
        "direction": direction,
        "day_type": day_type,
        "board": board,
        "alight": alight,
        "dep": clock_time(_norm_time(dep)),  # display only (map caption)
        "arr": clock_time(timed_stops[-1]["time"]),
        "route_color": route_color,
        "ghost_color": ghost_color,
        "ghost_opacity": 0.45,
        "geometry_source": geometry_source,
        "path": path,
        "stops": timed_stops,
        "board_index": board_index,
        "board_d": rider_from.get("d"),
        "ghost": {"lat": round(ghost[0], 5), "lon": round(ghost[1], 5)},
        "status": status_text(position, timed_stops),
        "outside_window": outside,
        "captions": captions,
        "now": now.strftime("%H:%M"),
        # The browser moves the ghost on its own: minutes since this
        # Cape Town midnight are the timetable clock, whatever the phone's
        # time zone.
        "service_midnight_ms": int(midnight.timestamp() * 1000),
    }


def _trip_stops(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    con: duckdb.DuckDBPyConnection,
    route_id: str,
    direction: str,
    day_type: str,
    board: str,
    alight: str,
    dep: str,
) -> Optional[tuple[list[dict[str, Any]], int]]:
    """The trip's stops with times, from as far back as it can be followed.

    Within one line the k-th time at every stop is the k-th trip, but only
    while every stop has the same number of trips. Walking back from the
    boarding stop, stops are added while that holds, so the result starts
    at the route's first stop when the timetable is regular, or at the
    boarding stop at worst. Returns (stops, index of the boarding stop), or
    None when the trip cannot be found.
    """
    names = [
        row[0] for row in con.execute(
            """
            SELECT   stop_name
            FROM     departures
            WHERE    route_id = ? AND direction = ? AND day_type = ?
            GROUP BY stop_name
            ORDER BY MIN(departure_time)
            """,
            [route_id, direction, day_type],
        ).fetchall()
    ]
    if board not in names or alight not in names:
        return None
    i, j = names.index(board), names.index(alight)
    if i >= j:
        return None
    times = _stop_times_lists(con, route_id, direction, day_type, names[:j + 1])
    n = len(times[board])
    idx = _find_trip_index(times[board], dep)
    if idx is None or any(len(times[name]) != n for name in names[i:j + 1]):
        return None

    start = i
    while start > 0 and len(times[names[start - 1]]) == n:
        earlier = _to_minutes(times[names[start - 1]][idx])
        if earlier > _to_minutes(times[names[start]][idx]):
            break  # not the same trip any more
        start -= 1

    stops = []
    for k in range(start, j + 1):
        name = names[k]
        role = "board" if k == i else "alight" if k == j else (
            "before" if k < i else "ride"
        )
        hms = _norm_time(times[name][idx])
        stops.append({"name": name, "time": hms, "t": _to_minutes(hms), "role": role})
    return stops, i - start


def _pin_stops_to_path(located: list[dict[str, Any]], path: list[list[float]],
                       snap: bool) -> None:
    """Give each located stop ``d``: its distance along *path*.

    On a street path each stop is pinned to the nearest vertex at or after
    the previous stop's, so stops stay in travel order even where the road
    doubles back. On a schematic path the stops are the vertices.
    """
    cumulative = [0.0]
    for a, b in zip(path, path[1:]):
        cumulative.append(cumulative[-1] + _segment(a, b))
    if not snap:
        for ts, dist in zip(located, cumulative):
            ts["d"] = dist
        return
    floor = 0
    for ts in located:
        point = [ts["lat"], ts["lon"]]
        best = min(
            range(floor, len(path)),
            key=lambda v, _p=point: _segment(path[v], _p),
        )
        ts["d"] = cumulative[best]
        floor = best


def _segment(a: list[float], b: list[float]) -> float:
    k = math.cos(math.radians(a[0]))
    return math.hypot(b[0] - a[0], (b[1] - a[1]) * k)


def ghost_position(located: list[dict[str, Any]], now_min: float) -> dict[str, Any]:
    """Where the timetable puts the bus at *now_min* (minutes since midnight).

    Returns {"state", "d", "prev", "next"}: state is not_started, moving or
    arrived; d is the distance along the path; prev and next are indexes
    into *located* around the bus. The browser copy of this lives in
    static/map/system_map.html (ghostPosition) and must stay the same.
    """
    first, last = located[0], located[-1]
    if now_min < first["t"]:
        return {"state": "not_started", "d": first["d"], "prev": 0, "next": 0}
    if now_min >= last["t"]:
        end = len(located) - 1
        return {"state": "arrived", "d": last["d"], "prev": end, "next": end}
    for k in range(len(located) - 1):
        a, b = located[k], located[k + 1]
        if a["t"] <= now_min < b["t"]:
            share = (now_min - a["t"]) / (b["t"] - a["t"])
            return {"state": "moving", "d": a["d"] + share * (b["d"] - a["d"]),
                    "prev": k, "next": k + 1}
    end = len(located) - 1
    return {"state": "arrived", "d": last["d"], "prev": end, "next": end}


def status_text(position: dict[str, Any], stops: list[dict[str, Any]]) -> str:
    """One plain sentence about where the bus should be (initial render)."""
    located = [ts for ts in stops if "d" in ts]
    board = next(ts for ts in stops if ts["role"] == "board")
    alight = stops[-1]
    if position["state"] == "not_started":
        first = located[0]
        return f"Not on the road yet. Leaves {first['name']} at {clock_time(first['time'])}."
    if position["state"] == "arrived":
        return f"Should have reached {alight['name']} at {clock_time(alight['time'])}."
    prev = located[position["prev"]]
    nxt = located[position["next"]]
    target = board if prev["t"] < board["t"] else alight
    return (f"Should be between {prev['name']} and {nxt['name']}. "
            f"Due at {target['name']} at {clock_time(target['time'])}.")


def _point_at_distance(path: list[list[float]], dist: float) -> list[float]:
    """The [lat, lon] *dist* along *path* (same units as _segment)."""
    walked = 0.0
    for a, b in zip(path, path[1:]):
        seg = _segment(a, b)
        if seg > 0 and walked + seg >= dist:
            t = (dist - walked) / seg
            return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
        walked += seg
    return list(path[-1])


# pylint: disable-next=too-many-arguments
def preview_ok_for_card(
    con: duckdb.DuckDBPyConnection,
    *,
    route_id: str,
    direction: str,
    day_type: str,
    board: str,
    alight: str,
    dep: str,
    arr: Optional[str] = None,
    memo: Optional[dict] = None,
) -> bool:
    """True when a result card may show the "Preview this trip" control."""
    ok, _ = eligibility(
        con=con,
        memo=memo,
        day_type=day_type,
        board=board,
        alight=alight,
        route_id=route_id,
        direction=direction,
        dep=dep,
        arr=arr,
    )
    return ok
