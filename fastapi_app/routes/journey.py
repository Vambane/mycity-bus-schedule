"""Dedicated journey schematic page (search cards navigate here)."""
from __future__ import annotations

import logging
from typing import Optional

import duckdb
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from fastapi_app.dependencies import get_connection
from fastapi_app.services.journey_page import JourneyParamError, build_journey_view
from fastapi_app.services.ls_service import get_effective_stage, stage_display_info
from fastapi_app.services.trust import trust_context

logger = logging.getLogger(__name__)

router = APIRouter()

_GENERIC_BAD = "That journey link is invalid or incomplete."
_GENERIC_MISS = "We could not rebuild that journey from the timetable."


@router.get("/journey", response_class=HTMLResponse)
async def journey_page(
    request: Request,
    from_stop: Optional[str] = Query(None),
    to_stop: Optional[str] = Query(None),
    day_type: Optional[str] = Query(None),
    route_id: Optional[str] = Query(None),
    direction: Optional[str] = Query(None),
    dep: Optional[str] = Query(None),
    arr: Optional[str] = Query(None),
    legs: Optional[str] = Query(None),
    operator: Optional[str] = Query("both"),
    conn: duckdb.DuckDBPyConnection = Depends(get_connection),
):
    """Full stop schematic for one itinerary — no Leaflet in v1."""
    templates = request.app.state.templates
    stage, stage_source = get_effective_stage()
    ls_stage = stage_display_info(stage, stage_source)
    trust = trust_context(conn)

    try:
        journey = build_journey_view(
            conn,
            from_stop=from_stop,
            to_stop=to_stop,
            day_type=day_type,
            route_id=route_id,
            direction=direction,
            dep=dep,
            arr=arr,
            legs_token=legs,
            operator=operator or "both",
        )
    except JourneyParamError as exc:
        msg = str(exc)
        # Map known "not found" style errors to 404; validation → 400.
        status = 404 if "unknown" in msg else 400
        detail = _GENERIC_MISS if status == 404 else _GENERIC_BAD
        logger.info("Journey page rejected (%s): %s", status, msg)
        return templates.TemplateResponse(
            request=request,
            name="pages/journey.html",
            status_code=status,
            context={
                "page": "journey",
                "error": detail,
                "journey": None,
                "ls_stage": ls_stage,
                **trust,
            },
        )
    except Exception:
        logger.exception("Journey page failed")
        return templates.TemplateResponse(
            request=request,
            name="pages/journey.html",
            status_code=400,
            context={
                "page": "journey",
                "error": _GENERIC_BAD,
                "journey": None,
                "ls_stage": ls_stage,
                **trust,
            },
        )

    return templates.TemplateResponse(
        request=request,
        name="pages/journey.html",
        context={
            "page": "journey",
            "error": None,
            "journey": journey,
            "ls_stage": ls_stage,
            **trust,
        },
    )
