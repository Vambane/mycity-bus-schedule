"""Neo-Brutalist Halte landing page at `/`."""
from __future__ import annotations

import logging
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from fastapi_app.dependencies import get_connection
from fastapi_app.services.trust import trust_context

logger = logging.getLogger(__name__)

router = APIRouter()

# Exact independence disclaimer (must match base.html app_disclaimer).
APP_DISCLAIMER = (
    "Halte is an independent app, not affiliated with the City of Cape Town, "
    "MyCiTi or the University of Cape Town."
)


def _marquee_routes(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """Real route labels from DB for the marquee (Jinja-escaped at render)."""
    rows = conn.execute(
        """
        SELECT route_id, route_name
        FROM routes
        ORDER BY
          CASE WHEN operator = 'myciti' THEN 0 ELSE 1 END,
          route_id
        """
    ).fetchall()
    labels: list[str] = []
    for route_id, route_name in rows:
        label = (route_name or "").strip() or str(route_id)
        # Normalise en-dash variants already in data; keep as plain text.
        labels.append(label)
    return labels


@router.get("/", response_class=HTMLResponse)
async def landing_page(
    request: Request,
    conn: duckdb.DuckDBPyConnection = Depends(get_connection),
):
    """Landing at `/`. Any query string → 301 to `/search` (same query)."""
    # Relative 301; query taken only from the request URL, never a param.
    query = request.url.query
    if query:
        return RedirectResponse(url=f"/search?{query}", status_code=301)

    templates = request.app.state.templates
    routes = _marquee_routes(conn)
    return templates.TemplateResponse(
        request=request,
        name="pages/landing.html",
        context={
            "page": "landing",
            "app_disclaimer": APP_DISCLAIMER,
            "marquee_routes": routes,
            **trust_context(conn),
        },
    )
