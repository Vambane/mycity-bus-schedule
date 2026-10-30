"""System map page route handler."""
import logging

import duckdb
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

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
    # Forward deliberate preview query params into the Leaflet iframe only.
    # Preview never starts from legend/stop click — only when ?preview=1&… is set.
    qs = str(request.url.query) if request.url.query else ""
    iframe_src = f"/static/map/system_map.html?v={request.app.state.asset_v}"
    if qs:
        iframe_src = f"{iframe_src}&{qs}"
    return templates.TemplateResponse(
        request=request,
        name="pages/system_map.html",
        context={
            "page": "map",
            "ls_stage": ls_stage,
            "map_iframe_src": iframe_src,
            **trust_context(conn),
        }
    )
