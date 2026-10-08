"""Journey search route handler."""
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
import duckdb

# Import core business logic
from journey import find_connections, find_transfer_connections
from disruption import assess_connection
import ls_ui
from fastapi_app.services.ls_service import get_effective_stage, stage_display_info
from journey_map import build_journey_map_data, _journey_map_html, leg_stop_sequence
from system_map import build_network, get_route_colors

logger = logging.getLogger(__name__)

router = APIRouter()


def get_cape_town_time():
    """Get current time in Cape Town timezone."""
    tz = zoneinfo.ZoneInfo(settings.timezone)
    return datetime.now(tz)


def get_current_day_type(dt: Optional[datetime] = None) -> str:
    """Determine day type from datetime."""
    if dt is None:
        dt = get_cape_town_time()

    weekday = dt.weekday()  # 0=Monday, 6=Sunday
    if weekday == 5:
        return "saturday"
    elif weekday == 6:
        return "sunday"
    else:
        return "weekday"


@router.get("/", response_class=HTMLResponse)
async def search_page(
    request: Request,
    from_stop: Optional[str] = None,
    to_stop: Optional[str] = None,
    day_type: Optional[str] = None,
    search_date: Optional[str] = None,  # Format: YYYY-MM-DD
    conn: duckdb.DuckDBPyConnection = Depends(get_connection)
):
    """
    Journey search page - main entry point.

    Args:
        from_stop: Origin stop name
        to_stop: Destination stop name (optional - redirects to stop view if empty)
        day_type: Service day type (weekday/saturday/sunday)
        conn: Database connection

    Returns:
        Rendered HTML page with search form and results
    """
    templates = request.app.state.templates

    # Get load shedding stage
    stage, stage_source = get_effective_stage()
    ls_stage = stage_display_info(stage, stage_source)

    # Get all stops for dropdown
    stops_query = "SELECT DISTINCT stop_name FROM stops ORDER BY stop_name"
    all_stops = [row[0] for row in conn.execute(stops_query).fetchall()]

    # Handle date selection
    search_date_parsed = None
    if search_date:
        try:
            from datetime import datetime as dt
            search_date_parsed = dt.strptime(search_date, "%Y-%m-%d").date()
            # Auto-determine day_type from date if not explicitly set
            if day_type is None:
                day_type = get_current_day_type(datetime.combine(search_date_parsed, datetime.min.time()))
        except ValueError:
            search_date = None  # Invalid date, ignore it

    # Determine day type
    if day_type is None:
        day_type = get_current_day_type()

    # Validate day type
    if day_type not in ["weekday", "saturday", "sunday"]:
        day_type = "weekday"

    # If no search yet, show empty form
    if not from_stop:
        return templates.TemplateResponse(
            request=request,
            name="pages/index.html",
            context={
                "page": "search",
                "all_stops": all_stops,
                "from_stop": "",
                "to_stop": "",
                "day_type": day_type,
                "search_date": search_date or "",
                "results": None,
                "ls_stage": ls_stage,
            }
        )

    # If only from_stop is selected, redirect to single-stop view
    if not to_stop or to_stop == from_stop:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/stop/{from_stop}?day_type={day_type}")

    try:
        # Get current time for filtering
        now = get_cape_town_time()

        # If a specific date is selected, start from beginning of day
        if search_date_parsed:
            current_time_str = "00:00:00"
            today = search_date_parsed
            time_context = f"date={search_date}"
        else:
            current_time_str = now.strftime("%H:%M:%S")
            today = now.date()
            time_context = f"time={current_time_str}"

        logger.info(f"Journey search: {from_stop} → {to_stop}, {day_type}, {time_context}")

        # Get block numbers for load shedding assessment (if available in DB)
        from_block = None
        to_block = None
        try:
            from_block_result = conn.execute(
                "SELECT ls_block FROM stops WHERE stop_name = ? LIMIT 1", [from_stop]
            ).fetchone()
            from_block = from_block_result[0] if from_block_result and from_block_result[0] else None

            to_block_result = conn.execute(
                "SELECT ls_block FROM stops WHERE stop_name = ? LIMIT 1", [to_stop]
            ).fetchone()
            to_block = to_block_result[0] if to_block_result and to_block_result[0] else None
        except Exception as e:
            # ls_block column doesn't exist - continue without block info
            logger.debug(f"Load shedding blocks not available: {e}")
            from_block = None
            to_block = None

        # Search for direct connections
        direct_df = find_connections(conn, from_stop, to_stop, day_type)

        # Filter for upcoming departures only
        showing_next_day = False
        if not direct_df.empty:
            upcoming = direct_df[direct_df["dep"] >= current_time_str].copy()

            # If no upcoming buses today, show tomorrow's first departures
            if upcoming.empty and len(direct_df) > 0:
                logger.info(f"No upcoming buses today, showing tomorrow's departures")
                direct_df = direct_df.head(10)  # Show first 10 departures of next day
                showing_next_day = True
            else:
                direct_df = upcoming.sort_values("dep").reset_index(drop=True)

        # Search for transfer connections if no direct routes
        transfer_df = None
        if direct_df.empty:
            logger.info("No direct connections, searching for transfers...")
            transfer_df = find_transfer_connections(conn, from_stop, to_stop, day_type)

            # Filter for upcoming departures
            if transfer_df is not None and not transfer_df.empty:
                upcoming_transfers = transfer_df[transfer_df["dep"] >= current_time_str].copy()

                # If no upcoming transfers today, show tomorrow's first transfers
                if upcoming_transfers.empty and len(transfer_df) > 0:
                    logger.info("No upcoming transfers today, showing tomorrow's departures")
                    transfer_df = transfer_df.head(10)
                    showing_next_day = True
                else:
                    transfer_df = upcoming_transfers.sort_values("dep").reset_index(drop=True)

        # Prepare results for template
        results = {
            "from_stop": from_stop,
            "to_stop": to_stop,
            "day_type": day_type,
            "search_date": search_date or "",
            "current_time": current_time_str[:5] if not search_date else "start of day",
            "has_results": False,
            "showing_next_day": showing_next_day,
            "direct": [],
            "transfers": [],
            "tabs": {
                "best": [],
                "fastest": [],
                "all_day": [],
            }
        }

        # Route colors for schematic display
        route_colors = get_route_colors(conn)

        # Process direct connections
        if not direct_df.empty:
            results["has_results"] = True

            # Convert to list of dicts for template
            direct_connections = []
            for _, row in direct_df.iterrows():
                # Assess load shedding disruption
                assessment = assess_connection(
                    row["dep"], row["arr"], from_block, to_block, stage, today
                )

                # Get UI helpers
                chip = ls_ui.connection_chip(assessment)
                caption = ls_ui.connection_caption(assessment, from_block, to_block)
                buffer = assessment.delay_buffer_min if assessment and assessment.affected else 0
                adjusted_duration = ls_ui.adjusted_duration_text(row["duration_min"], buffer)

                # Recover intermediate stop names for the schematic
                stops = leg_stop_sequence(
                    conn, row["route_id"], row.get("direction", ""),
                    from_stop, to_stop, day_type,
                )

                direct_connections.append({
                    "route_id": row["route_id"],
                    "route_name": row["route_name"],
                    "direction": row.get("direction", ""),
                    "dep": row["dep"][:5],  # HH:MM
                    "arr": row["arr"][:5],
                    "duration": row["duration"],
                    "duration_min": row["duration_min"],
                    "ls_affected": assessment.affected if assessment else False,
                    "ls_chip": chip,
                    "ls_caption": caption,
                    "ls_adjusted_duration": adjusted_duration,
                    "stops": stops,
                    "route_color": route_colors.get(row["route_id"], "#666"),
                })

            results["direct"] = direct_connections

            # Tab grouping: Best (first 8), Fastest (by duration), All day (all)
            results["tabs"]["best"] = direct_connections[:8]

            # Fastest: sort by duration, take first 8
            fastest = sorted(direct_connections, key=lambda x: x["duration_min"])[:8]
            results["tabs"]["fastest"] = fastest

            results["tabs"]["all_day"] = direct_connections

        # Process transfer connections
        if transfer_df is not None and not transfer_df.empty:
            results["has_results"] = True

            transfer_connections = []
            for _, row in transfer_df.iterrows():
                # legs is already a list[dict], route_ids is already a list[str],
                # via is already a list[str] — no parsing needed.
                legs_data = row.get("legs", []) or []
                route_ids = row.get("route_ids", []) or []
                via = row.get("via", []) or []

                # Recover intermediate stops for each leg of the transfer
                leg_stops = []
                leg_colors = []
                for leg in legs_data:
                    stops = leg_stop_sequence(
                        conn, leg["route_id"], leg.get("direction", ""),
                        leg["board"], leg["alight"], day_type,
                    )
                    leg_stops.append(stops)
                    leg_colors.append(route_colors.get(leg["route_id"], "#666"))

                transfer_connections.append({
                    "route_ids": route_ids,
                    "legs": legs_data,
                    "via": via,
                    "dep": row["dep"][:5],
                    "arr": row["arr"][:5],
                    "duration": row["duration"],
                    "duration_min": row["duration_min"],
                    "num_transfers": len(route_ids) - 1 if route_ids else 0,
                    "leg_stops": leg_stops,
                    "leg_colors": leg_colors,
                })

            results["transfers"] = transfer_connections

            # For transfers, add to tabs too
            if not direct_connections:
                results["tabs"]["best"] = transfer_connections[:8]
                results["tabs"]["fastest"] = sorted(transfer_connections, key=lambda x: x["duration_min"])[:8]
                results["tabs"]["all_day"] = transfer_connections

        return templates.TemplateResponse(
            request=request,
            name="pages/index.html",
            context={
                "page": "search",
                "all_stops": all_stops,
                "from_stop": from_stop,
                "to_stop": to_stop,
                "day_type": day_type,
                "search_date": search_date or "",
                "results": results,
                "ls_stage": ls_stage,
            }
        )

    except Exception as e:
        logger.error(f"Error in journey search: {e}", exc_info=True)
        return templates.TemplateResponse(
            request=request,
            name="pages/index.html",
            context={
                "page": "search",
                "all_stops": all_stops,
                "from_stop": from_stop,
                "to_stop": to_stop,
                "day_type": day_type,
                "search_date": search_date or "",
                "results": None,
                "error": f"Error searching for journeys: {str(e)}",
                "ls_stage": ls_stage,
            }
        )
