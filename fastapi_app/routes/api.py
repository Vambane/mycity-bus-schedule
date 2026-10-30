"""JSON API endpoints for AJAX requests."""
import logging
from typing import Any, Dict, Optional

import duckdb
from fastapi import APIRouter, Depends, HTTPException, Query

from fastapi_app.dependencies import get_connection
from fastapi_app.services.trip_preview import build_trip_preview
from system_map import build_network

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/map/network")
async def network_data(
    operator: Optional[str] = Query("both", pattern="^(both|myciti|uct)$"),
    conn: duckdb.DuckDBPyConnection = Depends(get_connection),
) -> Dict[str, Any]:
    """
    Get network graph data for the system map.

    Args:
        operator: 'both', 'myciti', or 'uct' — filter to one network.

    Returns:
        Dict containing nodes, links, routes, and paths for Leaflet rendering
    """
    try:
        logger.info(f"Building network graph (operator={operator})...")
        network = build_network(conn, operator=operator or "both")
        logger.info(
            f"Network graph built: {len(network.get('nodes', []))} nodes, "
            f"{len(network.get('routes', []))} routes"
        )
        return network
    except Exception as e:
        logger.error(f"Error building network data: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail=f"Failed to build network data: {e}"
        ) from e


@router.get("/stops")
async def list_stops(
    operator: Optional[str] = Query("both", pattern="^(both|myciti|uct)$"),
    conn: duckdb.DuckDBPyConnection = Depends(get_connection),
) -> Dict[str, Any]:
    """
    Get list of all stops for autocomplete/dropdown.

    Args:
        operator: 'both', 'myciti', or 'uct' — filter to one network.

    Returns:
        Dict with stops list
    """
    try:
        if operator and operator != "both":
            query = "SELECT DISTINCT stop_name FROM stops WHERE operator = ? ORDER BY stop_name"
            result = conn.execute(query, [operator]).fetchall()
        else:
            query = "SELECT DISTINCT stop_name FROM stops ORDER BY stop_name"
            result = conn.execute(query).fetchall()
        stops = [row[0] for row in result]
        return {"stops": stops}
    except Exception as e:
        logger.error(f"Error fetching stops: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch stops") from e


@router.get("/trip-preview")
# Each query parameter is a function argument in FastAPI.
# pylint: disable-next=too-many-arguments,too-many-positional-arguments
async def trip_preview(
    route_id: str = Query(..., min_length=1),
    direction: str = Query(..., min_length=1),
    day_type: str = Query(..., pattern="^(weekday|saturday|sunday)$"),
    board: str = Query(..., min_length=1),
    alight: str = Query(..., min_length=1),
    dep: str = Query(..., min_length=4),
    arr: Optional[str] = Query(None),
    conn: duckdb.DuckDBPyConnection = Depends(get_connection),
) -> Dict[str, Any]:
    """Timetable-interpolated ghost position for a deliberate trip preview.

    Never starts from legend/stop click — callers must pass an explicit trip.
    Hide reasons: soft pairing, stale scrape, wrong day, past last bus.
    """
    try:
        return build_trip_preview(
            conn,
            route_id=route_id,
            direction=direction,
            day_type=day_type,
            board=board,
            alight=alight,
            dep=dep,
            arr=arr,
        )
    except Exception as e:
        logger.error("Error building trip preview: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500, detail="Failed to build trip preview"
        ) from e
