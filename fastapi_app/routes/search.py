"""Journey search route handler."""
import logging
import zoneinfo
from datetime import datetime
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import ls_ui
from disruption import assess_connection
from fastapi_app.config import settings
from fastapi_app.dependencies import get_connection
from fastapi_app.services.journey_page import journey_href_direct, journey_href_transfer
from fastapi_app.services.ls_service import get_effective_stage, stage_display_info
from fastapi_app.services.service_day import (
    DAY_LABELS,
    day_type_for,
    holiday_name,
    is_live_board,
    next_service_day,
)
from fastapi_app.services.trip_preview import preview_ok_for_card
from fastapi_app.services.trust import trust_context
from fastapi_app.services.uct_validity import drop_expired, uct_expiry_state
# Import core business logic
from journey import find_connections, find_transfer_connections
from journey_map import leg_stop_sequence
from system_map import get_route_colors

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


def _without_expired_direct(df, expired_uct: set[str]):
    """Drop direct services on UCT routes whose timetable has expired."""
    if expired_uct and df is not None and not df.empty:
        return df[~df["route_id"].isin(expired_uct)].reset_index(drop=True)
    return df


def _without_expired_transfers(df, expired_uct: set[str]):
    """Drop transfer journeys that use any expired UCT route."""
    if expired_uct and df is not None and not df.empty:
        keep = df["route_ids"].apply(lambda ids: drop_expired(list(ids), expired_uct))
        return df[keep].reset_index(drop=True)
    return df


@router.get("/search", response_class=HTMLResponse)
# FastAPI maps query parameters to arguments, and this handler builds the
# whole results page in one pass. Splitting it is tracked as a refactor.
# pylint: disable-next=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,too-many-statements
async def search_page(
    request: Request,
    from_stop: Optional[str] = None,
    to_stop: Optional[str] = None,
    day_type: Optional[str] = None,
    search_date: Optional[str] = None,  # Format: YYYY-MM-DD
    operator: Optional[str] = None,     # 'both' | 'myciti' | 'uct'
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

    # Get all stops for dropdown, filtered by operator when specified
    if operator and operator != "both":
        stops_query = "SELECT DISTINCT stop_name FROM stops WHERE operator = ? ORDER BY stop_name"
        all_stops = [row[0] for row in conn.execute(stops_query, [operator]).fetchall()]
    else:
        stops_query = "SELECT DISTINCT stop_name FROM stops ORDER BY stop_name"
        all_stops = [row[0] for row in conn.execute(stops_query).fetchall()]

    # Handle date selection
    search_date_parsed = None
    if search_date:
        try:
            search_date_parsed = datetime.strptime(search_date, "%Y-%m-%d").date()
            # Auto-determine day_type from date if not explicitly set
            if day_type is None:
                day_type = get_current_day_type(
                    datetime.combine(search_date_parsed, datetime.min.time())
                )
        except ValueError:
            search_date = None  # Invalid date, ignore it

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
                "operator": operator,
                "results": None,
                "ls_stage": ls_stage,
                "uct_stale_warning": uct_stale_warning,
                **trust_context(conn),
            }
        )

    # If only from_stop is selected, redirect to single-stop view
    if not to_stop or to_stop == from_stop:
        return RedirectResponse(
            url=f"/stop/{from_stop}?day_type={day_type}&operator={operator}")

    try:
        # Get current time for filtering
        now = get_cape_town_time()

        # Live search: today's own timetable hides buses that have left.
        # A picked date, or another day's timetable, shows the whole day.
        live = is_live_board(day_type, now, search_date_parsed)
        today = search_date_parsed or now.date()
        if live:
            current_time_str = now.strftime("%H:%M:%S")
            time_context = f"time={current_time_str}"
        else:
            current_time_str = "00:00:00"
            time_context = f"date={search_date}" if search_date else "whole day"

        logger.info(f"Journey search: {from_stop} → {to_stop}, {day_type}, {time_context}")

        # Get block numbers for load shedding assessment (if available in DB)
        from_block = None
        to_block = None
        try:
            from_block_result = conn.execute(
                "SELECT ls_block FROM stops WHERE stop_name = ? LIMIT 1", [from_stop]
            ).fetchone()
            from_block = (
                from_block_result[0] if from_block_result and from_block_result[0] else None
            )

            to_block_result = conn.execute(
                "SELECT ls_block FROM stops WHERE stop_name = ? LIMIT 1", [to_stop]
            ).fetchone()
            to_block = to_block_result[0] if to_block_result and to_block_result[0] else None
        except Exception as e:
            # ls_block column doesn't exist - continue without block info
            logger.debug(f"Load shedding blocks not available: {e}")
            from_block = None
            to_block = None

        # Search for direct connections (scoped to selected operator)
        direct_df = _without_expired_direct(
            find_connections(conn, from_stop, to_stop, day_type, operator), expired_uct
        )

        # After the last bus, results switch to tomorrow's first departures
        # on tomorrow's own timetable (Friday night shows Saturday's).
        tomorrow, tomorrow_type = next_service_day(now)
        results_day_type = day_type
        showing_next_day = False

        # Direct services: upcoming only, else tomorrow's first ten
        if not direct_df.empty:
            upcoming = direct_df[direct_df["dep"] >= current_time_str]
            if not upcoming.empty:
                direct_df = upcoming.sort_values("dep").reset_index(drop=True)
            else:
                logger.info("No upcoming buses today, showing tomorrow's departures")
                if tomorrow_type != day_type:
                    direct_df = _without_expired_direct(
                        find_connections(conn, from_stop, to_stop, tomorrow_type, operator),
                        expired_uct,
                    )
                direct_df = direct_df.head(10)
                if not direct_df.empty:
                    showing_next_day = True
                    results_day_type = tomorrow_type

        # Transfers, only when there is no direct service at all
        transfer_df = None
        if direct_df.empty:
            logger.info("No direct connections, searching for transfers...")
            transfer_df = _without_expired_transfers(
                find_transfer_connections(conn, from_stop, to_stop, day_type, operator=operator),
                expired_uct,
            )
            if transfer_df is not None and not transfer_df.empty:
                upcoming_transfers = transfer_df[transfer_df["dep"] >= current_time_str]
                if not upcoming_transfers.empty:
                    transfer_df = upcoming_transfers.sort_values("dep").reset_index(drop=True)
                else:
                    logger.info("No upcoming transfers today, showing tomorrow's departures")
                    if tomorrow_type != day_type:
                        transfer_df = _without_expired_transfers(
                            find_transfer_connections(
                                conn, from_stop, to_stop, tomorrow_type, operator=operator
                            ),
                            expired_uct,
                        )
                    transfer_df = transfer_df.head(10)
                    if not transfer_df.empty:
                        showing_next_day = True
                        results_day_type = tomorrow_type

        # Prepare results for template
        results = {
            "from_stop": from_stop,
            "to_stop": to_stop,
            "day_type": results_day_type,
            "search_date": search_date or "",
            "current_time": current_time_str[:5] if not search_date else "start of day",
            "has_results": False,
            "showing_next_day": showing_next_day,
            "next_day_label": DAY_LABELS[results_day_type],
            "next_day_holiday": holiday_name(tomorrow) if showing_next_day else None,
            "today_holiday": holiday_name(today) if live else None,
            "live": live,
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

        # Initialize connections lists
        direct_connections = []

        # Process direct connections
        if not direct_df.empty:
            results["has_results"] = True

            # Convert to list of dicts for template
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
                    from_stop, to_stop, results_day_type,
                )

                preview_ok = preview_ok_for_card(
                    conn,
                    route_id=row["route_id"],
                    direction=row.get("direction", ""),
                    day_type=results_day_type,
                    board=from_stop,
                    alight=to_stop,
                    dep=row["dep"][:5],
                    arr=row["arr"][:5],
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
                    "preview_ok": preview_ok,
                    "journey_href": journey_href_direct(
                        from_stop=from_stop,
                        to_stop=to_stop,
                        day_type=results_day_type,
                        route_id=row["route_id"],
                        direction=row.get("direction", ""),
                        dep=row["dep"][:5],
                        arr=row["arr"][:5],
                        operator=operator or "both",
                    ),
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
                        leg["board"], leg["alight"], results_day_type,
                    )
                    leg_stops.append(stops)
                    leg_colors.append(route_colors.get(leg["route_id"], "#666"))

                # v1: preview first leg only (full multi-leg ghost is a follow-up)
                first = legs_data[0] if legs_data else None
                preview_ok = False
                preview_meta = {}
                if first:
                    preview_ok = preview_ok_for_card(
                        conn,
                        route_id=first["route_id"],
                        direction=first.get("direction", ""),
                        day_type=results_day_type,
                        board=first["board"],
                        alight=first["alight"],
                        dep=str(first.get("dep", ""))[:5],
                        arr=str(first.get("arr", ""))[:5] or None,
                    )
                    preview_meta = {
                        "preview_route_id": first["route_id"],
                        "preview_direction": first.get("direction", ""),
                        "preview_board": first["board"],
                        "preview_alight": first["alight"],
                        "preview_dep": str(first.get("dep", ""))[:5],
                    }
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
                    "preview_ok": preview_ok,
                    "journey_href": journey_href_transfer(
                        from_stop=from_stop,
                        to_stop=to_stop,
                        day_type=results_day_type,
                        legs=legs_data,
                        operator=operator or "both",
                    ),
                    **preview_meta,
                })

            results["transfers"] = transfer_connections

            # For transfers, add to tabs too
            if not direct_connections:
                results["tabs"]["best"] = transfer_connections[:8]
                results["tabs"]["fastest"] = sorted(
                    transfer_connections, key=lambda x: x["duration_min"]
                )[:8]
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
                "operator": operator,
                "results": results,
                "ls_stage": ls_stage,
                "uct_stale_warning": uct_stale_warning,
                **trust_context(
                    conn,
                    show_shedding_trust=any(
                        c.get("ls_affected") for c in results.get("direct", [])
                    ),
                ),
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
                "operator": operator,
                "results": None,
                "error": f"Error searching for journeys: {str(e)}",
                "ls_stage": ls_stage,
                "uct_stale_warning": uct_stale_warning,
                **trust_context(conn),
            }
        )
