"""Stop timetable view route handler."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from typing import Optional
import logging
import sys
from pathlib import Path
from datetime import datetime
import zoneinfo

# Add parent directory to path to import core modules
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from fastapi_app.dependencies import get_connection
from fastapi_app.config import settings
from fastapi_app.services.ls_service import get_effective_stage, stage_display_info
from disruption import _windows_min, _fmt_window
import ls_ui
import duckdb

logger = logging.getLogger(__name__)

router = APIRouter()


def get_cape_town_time():
    """Get current time in Cape Town timezone."""
    tz = zoneinfo.ZoneInfo(settings.timezone)
    return datetime.now(tz)


def get_current_day_type(dt: Optional[datetime] = None) -> str:
    """
    Determine day type (weekday/saturday/sunday) from datetime.

    Args:
        dt: Datetime to check (defaults to now)

    Returns:
        "weekday", "saturday", or "sunday"
    """
    if dt is None:
        dt = get_cape_town_time()

    weekday = dt.weekday()  # 0=Monday, 6=Sunday
    if weekday == 5:  # Saturday
        return "saturday"
    elif weekday == 6:  # Sunday
        return "sunday"
    else:
        return "weekday"


@router.get("/stop/{stop_name}", response_class=HTMLResponse)
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

        # Build operator SQL fragment used by queries below
        op_filter = ""
        op_params: list = []
        if operator != "both":
            op_filter = "AND d.operator = ?"
            op_params = [operator]

        # Check for stale UCT timetable data
        uct_stale_warning = None
        if operator in ("uct", "both"):
            try:
                row = conn.execute(
                    "SELECT MAX(valid_until) FROM timetables WHERE operator = 'uct'"
                ).fetchone()
                if row and row[0]:
                    from datetime import date
                    expiry = (row[0] if isinstance(row[0], date)
                              else date.fromisoformat(str(row[0])))
                    if expiry < date.today():
                        uct_stale_warning = (
                            f"UCT shuttle timetable expired on "
                            f"{expiry.strftime('%d %b %Y')}. Data may be outdated."
                        )
            except Exception:
                pass

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

        # Query routes serving this stop via the departures table.
        # The DB has no GTFS trips/calendar — day_type filtering is direct.
        # Optional operator filter narrows to one network.
        routes_query = f"""
        SELECT DISTINCT
            d.route_id,
            r.route_name,
            d.direction
        FROM departures d
        JOIN routes r ON d.route_id = r.route_id
        WHERE d.stop_name = ?
            AND d.day_type = ?
            {op_filter}
        ORDER BY r.route_name, d.direction
        """

        routes_result = conn.execute(
            routes_query,
            [stop_name, day_type] + op_params
        ).fetchall()

        if not routes_result:
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
                }
            )

        routes = [
            {
                "route_id": row[0],
                "route_name": row[1],
                "direction": row[2],
            }
            for row in routes_result
        ]

        # Query upcoming departures for each route+direction
        departures = {}
        for route in routes:
            departures_query = f"""
            SELECT
                d.departure_time,
                d.direction,
                r.route_name
            FROM departures d
            JOIN routes r ON d.route_id = r.route_id
            WHERE d.stop_name = ?
                AND d.route_id = ?
                AND d.direction = ?
                AND d.day_type = ?
                AND d.departure_time >= ?
                {op_filter}
            ORDER BY d.departure_time
            LIMIT 10
            """

            dep_result = conn.execute(
                departures_query,
                [stop_name, route["route_id"], route["direction"],
                 day_type, current_time] + op_params
            ).fetchall()

            route_departures = [
                {
                    "time": row[0][:5] if row[0] else "",  # HH:MM format
                    "headsign": row[1] or "",
                    "route_name": row[2],
                }
                for row in dep_result
            ]

            # Key by route_id + direction so multiple directions are separate
            dep_key = f"{route['route_id']}_{route['direction']}"

            if route_departures:
                departures[dep_key] = {
                    "route": route,
                    "next_departure": route_departures[0],
                    "upcoming": route_departures,
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
                "ls_stage": ls_stage,
                "ls_warning": ls_warning,
                "uct_stale_warning": uct_stale_warning,
            }
        )

    except Exception as e:
        logger.error(f"Error in stop view: {e}", exc_info=True)
        # Get load shedding stage even on error
        try:
            stage, stage_source = get_effective_stage()
            ls_stage = stage_display_info(stage, stage_source)
        except:
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
            }
        )
