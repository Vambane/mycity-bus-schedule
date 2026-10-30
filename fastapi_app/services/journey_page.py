"""Rebuild a shareable journey schematic from validated query params.

Design: search-journey-schematic.md — dedicated /journey page, not card expand.
"""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Optional
from urllib.parse import urlencode

import duckdb

from fastapi_app.services.trip_preview import preview_ok_for_card
from journey_map import leg_stop_sequence
from system_map import get_route_colors

logger = logging.getLogger(__name__)

_DAY_TYPES = frozenset({"weekday", "saturday", "sunday"})
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
# Route ids are short tokens (e.g. 105, T01, UCT-Jammie); reject path junk.
_ROUTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_STOP_MAX = 120
_LEGS_MAX = 4
_STOPS_MAX = 80


class JourneyParamError(ValueError):
    """Invalid or unsafe query — map to generic 400."""


def _clean_stop(name: Optional[str], *, field: str) -> str:
    if name is None or not str(name).strip():
        raise JourneyParamError(f"missing {field}")
    value = str(name).strip()
    if len(value) > _STOP_MAX:
        raise JourneyParamError(f"invalid {field}")
    if "\x00" in value or "/" in value or "\\" in value:
        raise JourneyParamError(f"invalid {field}")
    return value


def _clean_day(day_type: Optional[str]) -> str:
    value = (day_type or "").strip().lower()
    if value not in _DAY_TYPES:
        raise JourneyParamError("invalid day_type")
    return value


def _clean_time(value: Optional[str], *, field: str) -> str:
    if value is None or not str(value).strip():
        raise JourneyParamError(f"missing {field}")
    raw = str(value).strip()
    if not _TIME_RE.match(raw):
        raise JourneyParamError(f"invalid {field}")
    if len(raw) == 5:
        return raw
    return raw[:5]


def _clean_route(route_id: Optional[str]) -> str:
    value = (route_id or "").strip()
    if not _ROUTE_RE.match(value):
        raise JourneyParamError("invalid route_id")
    return value


def _clean_direction(direction: Optional[str]) -> str:
    value = (direction or "").strip()
    if not value or len(value) > _STOP_MAX:
        raise JourneyParamError("invalid direction")
    if "\x00" in value:
        raise JourneyParamError("invalid direction")
    return value


def encode_legs(legs: list[dict[str, Any]]) -> str:
    """URL-safe token for transfer legs (shareable query)."""
    compact = []
    for leg in legs:
        compact.append({
            "route_id": leg["route_id"],
            "direction": leg.get("direction", ""),
            "board": leg["board"],
            "alight": leg["alight"],
            "dep": str(leg.get("dep", ""))[:8],
            "arr": str(leg.get("arr", ""))[:8],
        })
    raw = json.dumps(compact, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_legs(token: str) -> list[dict[str, str]]:
    """Decode the base64 `legs` query token back into leg dicts.

    Raises JourneyParamError for anything malformed or oversized, so a
    hand-edited URL shows a friendly error rather than a crash.
    """
    if not token or len(token) > 4000:
        raise JourneyParamError("invalid legs")
    pad = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + pad)
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise JourneyParamError("invalid legs") from exc
    if not isinstance(data, list) or not data or len(data) > _LEGS_MAX:
        raise JourneyParamError("invalid legs")
    legs: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            raise JourneyParamError("invalid legs")
        legs.append({
            "route_id": _clean_route(item.get("route_id")),
            "direction": _clean_direction(item.get("direction")),
            "board": _clean_stop(item.get("board"), field="board"),
            "alight": _clean_stop(item.get("alight"), field="alight"),
            "dep": _clean_time(item.get("dep"), field="dep"),
            "arr": _clean_time(item.get("arr"), field="arr"),
        })
    return legs


def search_back_href(
    *,
    from_stop: str,
    to_stop: str,
    day_type: str,
    operator: str = "both",
) -> str:
    """Relative search URL only — never an open redirect."""
    op = operator if operator in ("both", "myciti", "uct") else "both"
    return "/search?" + urlencode({
        "from_stop": from_stop,
        "to_stop": to_stop,
        "day_type": day_type,
        "operator": op,
    })


def journey_href_direct(  # pylint: disable=too-many-arguments
    *,
    from_stop: str,
    to_stop: str,
    day_type: str,
    route_id: str,
    direction: str,
    dep: str,
    arr: str,
    operator: str = "both",
) -> str:
    """Link from a direct search result to its schematic page.

    Keyword-only on purpose: eight same-typed strings are too easy to
    pass in the wrong order positionally.
    """
    return "/journey?" + urlencode({
        "from_stop": from_stop,
        "to_stop": to_stop,
        "day_type": day_type,
        "route_id": route_id,
        "direction": direction,
        "dep": dep,
        "arr": arr,
        "operator": operator if operator in ("both", "myciti", "uct") else "both",
    })


def journey_href_transfer(
    *,
    from_stop: str,
    to_stop: str,
    day_type: str,
    legs: list[dict[str, Any]],
    operator: str = "both",
) -> str:
    """Link from a transfer search result to its schematic page (legs encoded)."""
    return "/journey?" + urlencode({
        "from_stop": from_stop,
        "to_stop": to_stop,
        "day_type": day_type,
        "legs": encode_legs(legs),
        "operator": operator if operator in ("both", "myciti", "uct") else "both",
    })


def _fmt_duration(dep: str, arr: str) -> str:
    def mins(t: str) -> int:
        parts = t.split(":")
        return int(parts[0]) * 60 + int(parts[1])

    d = mins(arr) - mins(dep)
    if d < 0:
        d += 24 * 60
    h, m = divmod(d, 60)
    if h:
        return f"{h}h {m}m" if m else f"{h}h"
    return f"{m} min"


def build_journey_view(  # pylint: disable=too-many-arguments
    con: duckdb.DuckDBPyConnection,
    *,
    from_stop: str,
    to_stop: str,
    day_type: str,
    route_id: Optional[str] = None,
    direction: Optional[str] = None,
    dep: Optional[str] = None,
    arr: Optional[str] = None,
    legs_token: Optional[str] = None,
    operator: str = "both",
) -> dict[str, Any]:
    """Validate params and rebuild stop lists for the schematic page."""
    from_stop = _clean_stop(from_stop, field="from_stop")
    to_stop = _clean_stop(to_stop, field="to_stop")
    day_type = _clean_day(day_type)
    op = operator if operator in ("both", "myciti", "uct") else "both"

    colors = get_route_colors(con)

    if legs_token:
        legs = decode_legs(legs_token)
        if legs[0]["board"] != from_stop or legs[-1]["alight"] != to_stop:
            raise JourneyParamError("legs mismatch")
        leg_stops: list[list[str]] = []
        leg_colors: list[str] = []
        route_ids: list[str] = []
        for leg in legs:
            stops = leg_stop_sequence(
                con,
                leg["route_id"],
                leg["direction"],
                leg["board"],
                leg["alight"],
                day_type,
            )
            if len(stops) < 2:
                raise JourneyParamError("unknown journey")
            if len(stops) > _STOPS_MAX:
                raise JourneyParamError("invalid journey")
            leg_stops.append(stops)
            leg_colors.append(colors.get(leg["route_id"], "#666"))
            route_ids.append(leg["route_id"])

        first = legs[0]
        preview_ok = preview_ok_for_card(
            con,
            route_id=first["route_id"],
            direction=first["direction"],
            day_type=day_type,
            board=first["board"],
            alight=first["alight"],
            dep=first["dep"],
            arr=first["arr"],
        )
        return {
            "kind": "transfer",
            "from_stop": from_stop,
            "to_stop": to_stop,
            "day_type": day_type,
            "operator": op,
            "dep": legs[0]["dep"],
            "arr": legs[-1]["arr"],
            "duration": _fmt_duration(legs[0]["dep"], legs[-1]["arr"]),
            "route_ids": route_ids,
            "legs": legs,
            "leg_stops": leg_stops,
            "leg_colors": leg_colors,
            "via": [leg["board"] for leg in legs[1:]],
            "num_transfers": max(0, len(legs) - 1),
            "preview_ok": preview_ok,
            "preview_route_id": first["route_id"],
            "preview_direction": first["direction"],
            "preview_board": first["board"],
            "preview_alight": first["alight"],
            "preview_dep": first["dep"],
            "back_href": search_back_href(
                from_stop=from_stop, to_stop=to_stop, day_type=day_type, operator=op,
            ),
        }

    # Direct
    route_id = _clean_route(route_id)
    direction = _clean_direction(direction)
    dep = _clean_time(dep, field="dep")
    arr = _clean_time(arr, field="arr")
    stops = leg_stop_sequence(
        con, route_id, direction, from_stop, to_stop, day_type,
    )
    if len(stops) < 2:
        raise JourneyParamError("unknown journey")
    if len(stops) > _STOPS_MAX:
        raise JourneyParamError("invalid journey")

    # Confirm route exists for this day (avoid fabricating journeys)
    exists = con.execute(
        """
        SELECT 1 FROM departures
        WHERE route_id = ? AND direction = ? AND day_type = ?
              AND stop_name = ?
        LIMIT 1
        """,
        [route_id, direction, day_type, from_stop],
    ).fetchone()
    if not exists:
        raise JourneyParamError("unknown journey")

    route_name_row = con.execute(
        "SELECT route_name FROM routes WHERE route_id = ? LIMIT 1",
        [route_id],
    ).fetchone()
    route_name = route_name_row[0] if route_name_row else route_id
    route_color = colors.get(route_id, "#666")
    preview_ok = preview_ok_for_card(
        con,
        route_id=route_id,
        direction=direction,
        day_type=day_type,
        board=from_stop,
        alight=to_stop,
        dep=dep,
        arr=arr,
    )
    return {
        "kind": "direct",
        "from_stop": from_stop,
        "to_stop": to_stop,
        "day_type": day_type,
        "operator": op,
        "route_id": route_id,
        "route_name": route_name,
        "direction": direction,
        "dep": dep,
        "arr": arr,
        "duration": _fmt_duration(dep, arr),
        "stops": stops,
        "route_color": route_color,
        "route_ids": [route_id],
        "preview_ok": preview_ok,
        "preview_route_id": route_id,
        "preview_direction": direction,
        "preview_board": from_stop,
        "preview_alight": to_stop,
        "preview_dep": dep,
        "back_href": search_back_href(
            from_stop=from_stop, to_stop=to_stop, day_type=day_type, operator=op,
        ),
    }
