"""System map page route handler."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
import logging
import duckdb

from fastapi_app.dependencies import get_connection
from fastapi_app.services.ls_service import get_effective_stage, stage_display_info
from fastapi_app.services.trust import trust_context

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/map", response_class=HTMLResponse)
async def map_page(
    request: Request,
    conn: duckdb.DuckDBPyConnection = Depends(get_connection),
):
    """
    Display interactive system map page.

    Returns:
        Rendered HTML page with embedded Leaflet map
    """
    # Get load shedding stage
    stage, stage_source = get_effective_stage()
    ls_stage = stage_display_info(stage, stage_source)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request=request,
        name="pages/system_map.html",
        context={
            "page": "map",
            "ls_stage": ls_stage,
            **trust_context(conn),
        }
    )
