"""JSON API endpoints for AJAX requests."""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Dict, Any, Optional
import logging
import sys
from pathlib import Path

# Add parent directory to path to import core modules
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from fastapi_app.dependencies import get_connection
import duckdb

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
        from system_map import build_network
        logger.info(f"Building network graph (operator={operator})...")
        network = build_network(conn, operator=operator or "both")
        logger.info(
            f"Network graph built: {len(network.get('nodes', []))} nodes, "
            f"{len(network.get('routes', []))} routes"
        )
        return network
    except Exception as e:
        logger.error(f"Error building network data: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to build network data: {str(e)}")


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
        raise HTTPException(status_code=500, detail="Failed to fetch stops")
