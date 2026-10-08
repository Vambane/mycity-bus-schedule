"""FastAPI application entry point."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import logging
from pathlib import Path

from .config import settings
from .dependencies import init_connection_pool, close_connection_pool

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


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
    version="2.0.0",
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
from .routes import api, search, stop, system_map

app.include_router(api.router, prefix="/api", tags=["API"])
app.include_router(search.router, tags=["Search"])
app.include_router(stop.router, tags=["Stop"])
app.include_router(system_map.router, tags=["Map"])


@app.get("/health")
async def health_check():
    """Health check endpoint for deployment monitoring."""
    return {"status": "ok", "version": "2.0.0"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "fastapi_app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.reload,
        log_level=settings.log_level.lower(),
    )
