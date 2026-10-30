"""Trip preview (ghost bus) — timetable interpolation, deliberate trigger only.

Design: /workspace/mycity-team/design/trip-preview-interaction.md
Compliance caption (exact): Estimated from timetable — not live GPS.
"""
from __future__ import annotations

import logging
import math
import zoneinfo
from datetime import datetime, timedelta
from typing import Any, Optional

import duckdb

from fastapi_app.config import settings
from fastapi_app.services.trust import get_scraped_at_raw
from journey import clock_time
from journey_map import (
    _load_route_paths,
    _load_stop_coords,
    _match_coords,
    _street_slice,
    leg_stop_sequence,
)
from system_map import get_route_colors

logger = logging.getLogger(__name__)

COMPLIANCE_CAPTION = "Estimated from timetable — not live GPS."
OPTIONAL_CAPTION = (
    "Planning aid only. Confirm on MyCiTi / UCT before you travel."
)
OUTSIDE_WINDOW_CAPTION = "Outside trip window — showing scheduled start/end"

# Soft staleness: hide preview when scrape is older than this (calendar days).
# Generous so a labelled scrape still previews; missing scrape always hides.
_STALE_MAX_DAYS = 45


def _tz() -> zoneinfo.ZoneInfo:
    return zoneinfo.ZoneInfo(settings.timezone)


def _now_ct() -> datetime:
    return datetime.now(_tz())


def _day_type_for(dt: datetime) -> str:
    weekday = dt.weekday()
    if weekday == 5:
        return "saturday"
    if weekday == 6:
        return "sunday"
    return "weekday"


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
    day_type: str,
    board: str,
    alight: str,
    route_id: str,
    direction: str,
    dep: str,
    arr: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Return (ok, hide_reason). Reasons are internal; button is hidden when not ok."""
    scraped = get_scraped_at_raw(con)
    if scraped is None:
        return False, "stale"
    now = _now_ct()
    scraped_aware = scraped if scraped.tzinfo else scraped.replace(tzinfo=_tz())
    if now - scraped_aware > timedelta(days=_STALE_MAX_DAYS):
        return False, "stale"

    if day_type != _day_type_for(now):
        return False, "wrong_day"

    seq = leg_stop_sequence(con, route_id, direction, board, alight, day_type)
    if len(seq) < 2:
        return False, "no_geometry"

    times = _stop_times_lists(con, route_id, direction, day_type, seq)
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

    trip_arr = arr
    if not trip_arr:
        alight_times = times.get(alight) or []
        trip_arr = alight_times[idx] if idx < len(alight_times) else None
    if trip_arr:
        now_min = now.hour * 60 + now.minute
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

    seq = leg_stop_sequence(con, route_id, direction, board, alight, day_type)
    times = _stop_times_lists(con, route_id, direction, day_type, seq)
    idx = _find_trip_index(times[board], dep)
    assert idx is not None  # gated by eligibility

    timed_stops: list[dict[str, Any]] = []
    for stop in seq:
        timed_stops.append({
            "name": stop,
            "time": _norm_time(times[stop][idx]),
        })

    coords = _match_coords(seq, _load_stop_coords())
    for ts in timed_stops:
        if ts["name"] in coords:
            lat, lon = coords[ts["name"]]
            ts["lat"] = round(lat, 5)
            ts["lon"] = round(lon, 5)

    schematic_pts = [
        [ts["lat"], ts["lon"]] for ts in timed_stops if "lat" in ts
    ]
    if len(schematic_pts) < 2:
        return {**base, "eligible": False, "hide_reason": "no_geometry"}

    street = _street_slice(
        _load_route_paths().get(route_id.upper(), []),
        schematic_pts[0],
        schematic_pts[-1],
    )
    geometry_source = "street" if street is not None else "schematic"
    path = street if street is not None else schematic_pts

    colors = get_route_colors(con)
    route_color = colors.get(route_id, "#666666")
    ghost_color = _mute_color(route_color)

    now = _now_ct()
    now_min = now.hour * 60 + now.minute
    start_min = _to_minutes(timed_stops[0]["time"])
    end_min = _to_minutes(timed_stops[-1]["time"])

    outside = False
    if now_min < start_min:
        fraction = 0.0
        outside = True
    elif now_min >= end_min:
        fraction = 1.0
        outside = True
    else:
        # Time fraction between consecutive stops, then along path by
        # stop-index span (approx; street path not stop-aligned).
        fraction = 0.0
        for i in range(len(timed_stops) - 1):
            a_m = _to_minutes(timed_stops[i]["time"])
            b_m = _to_minutes(timed_stops[i + 1]["time"])
            if now_min < a_m:
                break
            if now_min <= b_m and b_m > a_m:
                local = (now_min - a_m) / (b_m - a_m)
                # Map stop index span onto path length fraction
                stop_frac_a = i / (len(timed_stops) - 1)
                stop_frac_b = (i + 1) / (len(timed_stops) - 1)
                fraction = stop_frac_a + local * (stop_frac_b - stop_frac_a)
                break
            fraction = (i + 1) / (len(timed_stops) - 1)

    ghost = _point_along(path, fraction)
    captions = [COMPLIANCE_CAPTION, OPTIONAL_CAPTION]
    if outside:
        captions.insert(0, OUTSIDE_WINDOW_CAPTION)

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
        "ghost": {"lat": round(ghost[0], 5), "lon": round(ghost[1], 5)},
        "outside_window": outside,
        "captions": captions,
        "now": now.strftime("%H:%M"),
    }


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
) -> bool:
    """True when a result card may show the "Preview this trip" control."""
    ok, _ = eligibility(
        con=con,
        day_type=day_type,
        board=board,
        alight=alight,
        route_id=route_id,
        direction=direction,
        dep=dep,
        arr=arr,
    )
    return ok
