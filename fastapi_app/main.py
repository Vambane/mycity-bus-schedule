"""FastAPI application entry point."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import logging
from pathlib import Path

from .config import settings
from .dependencies import (
    init_connection_pool,
    close_connection_pool,
    is_pool_ready,
    get_connection,
)

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

APP_VERSION = "2.0.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    logger.info("Starting MyCiTi Bus Timetable application...")
    init_connection_pool()
    yield
    # Shutdown
    logger.info("Shutting down application...")
    close_connection_pool()


# Create FastAPI app
app = FastAPI(
    title="MyCiTi Bus Timetable",
    description="Journey planning and timetable system for MyCiTi buses in Cape Town",
    version=APP_VERSION,
    lifespan=lifespan,
)

# Mount static files
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Setup Jinja2 templates
templates_dir = Path(__file__).parent / "templates"
# Disable Jinja2 auto-escaping for performance and compatibility
from jinja2 import Environment, FileSystemLoader
jinja_env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=True, cache_size=0)
templates = Jinja2Templates(env=jinja_env)

# Make templates available globally
app.state.templates = templates

# Import and register routes
from .routes import api, search, stop, system_map, journey

app.include_router(api.router, prefix="/api", tags=["API"])
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
    from .services.trust import get_scraped_at_display, get_scraped_at_raw

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
