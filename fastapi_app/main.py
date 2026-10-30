"""FastAPI application entry point."""
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader

from journey import clock_time

from .config import settings
from .dependencies import close_connection_pool, get_connection, init_connection_pool, is_pool_ready
from .routes import api, journey, landing, search, stop, system_map
from .services.trust import get_scraped_at_display, get_scraped_at_raw
from .services.uct_notes import uct_notes

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

APP_VERSION = "2.0.0"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Application lifespan events."""
    # Startup
    logger.info("Starting Halte application...")
    init_connection_pool()
    yield
    # Shutdown
    logger.info("Shutting down application...")
    close_connection_pool()


# Create FastAPI app
app = FastAPI(
    title="Halte",
    description="Journey planning and timetable system for MyCiTi buses in Cape Town",
    version=APP_VERSION,
    lifespan=lifespan,
)

# Mount static files
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Setup Jinja2 templates
templates_dir = Path(__file__).parent / "templates"
# Auto-escaping stays on. Templates are compiled once and cached; Jinja's
# auto_reload (on by default) still picks up edited files without a restart.
# (cache_size=0 used to recompile the result-card template once per card.)
jinja_env = Environment(
    loader=FileSystemLoader(str(templates_dir)), autoescape=True, auto_reload=True
)
jinja_env.globals["uct_notes"] = uct_notes
# Display times as wall-clock HH:MM (stored overnight times run past 24:00).
jinja_env.filters["clock"] = clock_time
templates = Jinja2Templates(env=jinja_env)

# Cache-busting version for local static assets: short git SHA on Render,
# otherwise a startup timestamp (changes on every restart).
ASSET_V = (os.environ.get("RENDER_GIT_COMMIT") or "")[:7] or str(int(time.time()))
jinja_env.globals["asset_v"] = ASSET_V
app.state.asset_v = ASSET_V


@app.middleware("http")
async def cache_control_headers(request, call_next):
    """no-cache on HTML and /static so browsers (iOS Safari) always revalidate.

    Static stays no-cache rather than immutable: unversioned /static URLs can
    still be requested, so long-lived caching is not guaranteed safe.
    """
    response = await call_next(request)
    ctype = response.headers.get("content-type", "")
    if request.url.path.startswith("/static/") or ctype.startswith("text/html"):
        response.headers["Cache-Control"] = "no-cache"
    return response

# Make templates available globally
app.state.templates = templates

# Register routes
app.include_router(api.router, prefix="/api", tags=["API"])
app.include_router(landing.router, tags=["Landing"])
app.include_router(search.router, tags=["Search"])
app.include_router(stop.router, tags=["Stop"])
app.include_router(system_map.router, tags=["Map"])
app.include_router(journey.router, tags=["Journey"])


@app.get("/health")
async def health_check():
    """
    Fast health check for deployment monitors.

    Does not run ETL. Returns non-200 / degraded when the DuckDB pool or
    snapshot-backed data is unavailable (fail-loud for empty boards).
    """
    db_ready = is_pool_ready()
    scraped_at = None
    scraped_display = None
    status = "ok"

    if not db_ready:
        status = "degraded"
        body = {
            "status": status,
            "version": APP_VERSION,
            "db_ready": False,
            "scraped_at": None,
        }
        return JSONResponse(status_code=503, content=body)

    try:
        conn = get_connection()
        raw = get_scraped_at_raw(conn)
        scraped_at = raw.isoformat(sep=" ", timespec="seconds") if raw else None
        scraped_display = get_scraped_at_display(conn)
        # Cheap readiness probe — must not touch PDF ETL.
        conn.execute("SELECT 1").fetchone()
        if raw is None:
            status = "degraded"
    except Exception as exc:  # noqa: BLE001
        logger.error("Health check DB probe failed: %s", exc)
        status = "degraded"
        db_ready = False

    body = {
        "status": status,
        "version": APP_VERSION,
        "db_ready": db_ready,
        "scraped_at": scraped_at,
        "scraped_at_display": scraped_display,
    }
    if status != "ok":
        return JSONResponse(status_code=503, content=body)
    return body


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "fastapi_app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.reload,
        log_level=settings.log_level.lower(),
    )
