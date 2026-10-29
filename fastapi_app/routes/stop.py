"""Stop timetable view route handler."""
import logging
import zoneinfo
from datetime import datetime
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

import ls_ui
from disruption import _fmt_window, _windows_min
from fastapi_app.config import settings
from fastapi_app.dependencies import get_connection
from fastapi_app.services.ls_service import get_effective_stage, stage_display_info
from fastapi_app.services.service_day import (
    DAY_LABELS,
    day_type_for,
    holiday_name,
    is_live_board,
    next_service_day,
)
from fastapi_app.services.trust import trust_context
from fastapi_app.services.uct_validity import uct_expiry_state

logger = logging.getLogger(__name__)

router = APIRouter()


def get_cape_town_time():
    """Get current time in Cape Town timezone."""
    tz = zoneinfo.ZoneInfo(settings.timezone)
    return datetime.now(tz)


def get_current_day_type(dt: Optional[datetime] = None) -> str:
    """Timetable that runs on *dt* (default: now): weekday, saturday or sunday.

    Public holidays run the Sunday timetable (see services/service_day.py).
    """
    if dt is None:
        dt = get_cape_town_time()
    return day_type_for(dt.date())


def _load_board(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    conn: duckdb.DuckDBPyConnection,
    stop_name: str,
    day_type: str,
    from_time: str,
    operator: str,
    expired_uct: set[str],
) -> tuple[list[dict], dict]:
    """Routes serving *stop_name* on *day_type*, and each one's next 10 departures.

    Departures before *from_time* (HH:MM:SS) are left out. Routes with no
    departures left are kept in the route list but have no board entry.
    """
    op_filter = "AND d.operator = ?" if operator != "both" else ""
    op_params: list = [operator] if operator != "both" else []

    # The DB has no GTFS trips/calendar: day_type filtering is direct.
    routes_result = conn.execute(
        f"""
        SELECT DISTINCT d.route_id, r.route_name, d.direction
        FROM departures d
        JOIN routes r ON d.route_id = r.route_id
        WHERE d.stop_name = ? AND d.day_type = ? {op_filter}
        ORDER BY r.route_name, d.direction
        """,
        [stop_name, day_type] + op_params,
    ).fetchall()
    routes = [
        {"route_id": row[0], "route_name": row[1], "direction": row[2]}
        for row in routes_result
        if row[0] not in expired_uct
    ]

    departures: dict = {}
    for route in routes:
        dep_result = conn.execute(
            f"""
            SELECT d.departure_time, d.direction, r.route_name
            FROM departures d
            JOIN routes r ON d.route_id = r.route_id
            WHERE d.stop_name = ? AND d.route_id = ? AND d.direction = ?
                AND d.day_type = ? AND d.departure_time >= ? {op_filter}
            ORDER BY d.departure_time
            LIMIT 10
            """,
            [stop_name, route["route_id"], route["direction"], day_type, from_time]
            + op_params,
        ).fetchall()
        route_departures = [
            {
                "time": row[0][:5] if row[0] else "",  # HH:MM
                "headsign": row[1] or "",
                "route_name": row[2],
            }
            for row in dep_result
        ]
        if route_departures:
            # Key by route_id + direction so multiple directions are separate
            departures[f"{route['route_id']}_{route['direction']}"] = {
                "route": route,
                "next_departure": route_departures[0],
                "upcoming": route_departures,
            }
    return routes, departures


@router.get("/stop/{stop_name}", response_class=HTMLResponse)
# One handler builds the whole departure board; splitting it is tracked as a refactor.
# pylint: disable-next=too-many-locals,too-many-branches,too-many-statements
async def stop_view(
    request: Request,
    stop_name: str,
    day_type: Optional[str] = None,
    operator: Optional[str] = None,  # 'both' | 'myciti' | 'uct'
    conn: duckdb.DuckDBPyConnection = Depends(get_connection)
):
    """
    Display timetable for a specific stop.

    Args:
        stop_name: Name of the stop
        day_type: Optional day type filter (weekday/saturday/sunday)
        conn: Database connection

    Returns:
        Rendered HTML page
    """
    try:
        # Determine day type
        if day_type is None:
            day_type = get_current_day_type()

        # Validate day type
        if day_type not in ["weekday", "saturday", "sunday"]:
            day_type = "weekday"

        # Validate operator filter (default: show both networks)
        if operator not in ("both", "myciti", "uct"):
            operator = "both"

        # Per-route UCT validity: expired routes are hidden and the banner shown
        expired_uct: set[str] = set()
        uct_stale_warning = None
        if operator in ("uct", "both"):
            expired_uct, uct_stale_warning = uct_expiry_state(conn)

        # Get current time for filtering upcoming departures
        now = get_cape_town_time()
        current_time = now.strftime("%H:%M:%S")
        today = now.date()

        logger.info(f"Stop view: {stop_name}, day_type={day_type}, time={current_time}")

        # Get load shedding stage
        stage, stage_source = get_effective_stage()
        ls_stage = stage_display_info(stage, stage_source)

        # Get stop's load shedding block (if available in DB)
        stop_block = None
        try:
            block_result = conn.execute(
                "SELECT ls_block FROM stops WHERE stop_name = ? LIMIT 1", [stop_name]
            ).fetchone()
            stop_block = block_result[0] if block_result and block_result[0] else None
        except Exception as e:
            # ls_block column doesn't exist - continue without block info
            logger.debug(f"Load shedding blocks not available: {e}")
            stop_block = None

        # Get load shedding windows for this stop today
        ls_warning = None
        if stop_block and stage > 0:
            windows_min = _windows_min(stop_block, stage, today)
            if windows_min:
                windows_str = [_fmt_window(start, end) for start, end in windows_min[:3]]
                ls_warning = ls_ui.stop_banner_text(stop_name, stop_block, windows_str)

        # Live board: today's own timetable hides buses that have left.
        # Any other day type shows the whole day from the first bus.
        live = is_live_board(day_type, now)
        routes, departures = _load_board(
            conn, stop_name, day_type, current_time if live else "00:00:00",
            operator, expired_uct,
        )

        if not routes:
            # No routes found for this stop
            templates = request.app.state.templates
            return templates.TemplateResponse(
                request=request,
                name="pages/stop.html",
                context={
                    "page": "stop",
                    "stop_name": stop_name,
                    "day_type": day_type,
                    "operator": operator,
                    "routes": [],
                    "departures": {},
                    "error": f"No routes found for stop '{stop_name}' on {day_type}s",
                    "ls_stage": ls_stage,
                    "uct_stale_warning": uct_stale_warning,
                    **trust_context(conn),
                }
            )

        # After the last bus, show tomorrow's first departures on
        # tomorrow's own timetable (Friday night shows Saturday's).
        board_day_type = day_type
        next_day = None
        if live and not departures:
            tomorrow, tomorrow_type = next_service_day(now)
            t_routes, t_departures = _load_board(
                conn, stop_name, tomorrow_type, "00:00:00", operator, expired_uct
            )
            if t_departures:
                routes, departures = t_routes, t_departures
                board_day_type = tomorrow_type
                next_day = {
                    "day_type": tomorrow_type,
                    "label": DAY_LABELS[tomorrow_type],
                    "holiday": holiday_name(tomorrow),
                }

        templates = request.app.state.templates
        return templates.TemplateResponse(
            request=request,
            name="pages/stop.html",
            context={
                "page": "stop",
                "stop_name": stop_name,
                "day_type": day_type,
                "operator": operator,
                "routes": routes,
                "departures": departures,
                "current_time": current_time[:5],
                "live": live,
                "board_day_type": board_day_type,
                "next_day": next_day,
                "today_holiday": holiday_name(today),
                "ls_stage": ls_stage,
                "ls_warning": ls_warning,
                "uct_stale_warning": uct_stale_warning,
                **trust_context(conn, show_shedding_trust=bool(ls_warning)),
            }
        )

    except Exception as e:
        logger.error(f"Error in stop view: {e}", exc_info=True)
        # Get load shedding stage even on error
        try:
            stage, stage_source = get_effective_stage()
            ls_stage = stage_display_info(stage, stage_source)
        except Exception:  # never let the badge break the error page
            ls_stage = None

        templates = request.app.state.templates
        return templates.TemplateResponse(
            request=request,
            name="pages/stop.html",
            context={
                "page": "stop",
                "stop_name": stop_name,
                "day_type": day_type or "weekday",
                "operator": operator or "both",
                "routes": [],
                "departures": {},
                "error": f"Error loading stop data: {str(e)}",
                "ls_stage": ls_stage,
                **trust_context(conn),
            }
        )
