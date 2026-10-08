"""
Load shedding service for FastAPI.

Adapted from loadshedding.py - removes Streamlit dependencies,
uses standard caching and environment variables.
"""
import logging
import os
from typing import Optional, Tuple
import requests
from cachetools import TTLCache, cached
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ESP_STATUS_URL = "https://developer.sepush.co.za/business/2.0/status"
REQUEST_TIMEOUT_S = 5

# Badge colors per stage band: (background, foreground)
STAGE_COLORS = {
    "green": ("#e6f4ea", "#137333"),      # stage 0: no load shedding
    "amber": ("#fef7e0", "#b45309"),      # stages 1-2: mild
    "red": ("#fce8e6", "#c5221f"),        # stages 3-4: serious
    "dark_red": ("#f3d6d6", "#8b0000"),   # stages 5+: severe
}

# Cache for ESP API calls (30 min TTL = 48 calls/day, under 50/day free tier)
_esp_cache = TTLCache(maxsize=1, ttl=1800)


# ---------------------------------------------------------------------------
# Stage source
# ---------------------------------------------------------------------------

def _get_api_key() -> Optional[str]:
    """Return the ESP API key from environment variables."""
    return os.environ.get("ESP_API_KEY") or None


def _fetch_esp_stage_uncached() -> Optional[int]:
    """
    Fetch the current national Eskom stage from the ESP API.

    Returns:
        The stage as an int in 0..8, or None on any failure: missing API
        key, network error, HTTP error, malformed payload, or an
        out-of-range stage value. Never raises.
    """
    key = _get_api_key()
    if key is None:
        logger.debug("No ESP API key configured")
        return None

    try:
        resp = requests.get(
            ESP_STATUS_URL,
            headers={"token": key},
            timeout=REQUEST_TIMEOUT_S,
        )
        resp.raise_for_status()
        stage = int(resp.json()["status"]["eskom"]["stage"])
    except Exception as exc:
        logger.warning(f"ESP stage fetch failed: {exc}")
        return None

    if not 0 <= stage <= 8:
        logger.warning(f"ESP returned out-of-range stage {stage}; ignoring")
        return None

    logger.info(f"ESP API returned stage {stage}")
    return stage


@cached(_esp_cache)
def fetch_esp_stage() -> Optional[int]:
    """
    Cached wrapper around the ESP stage fetch.

    Cache TTL is 30 minutes to stay under API rate limits.
    """
    return _fetch_esp_stage_uncached()


# ---------------------------------------------------------------------------
# Stage resolution
# ---------------------------------------------------------------------------

def _resolve_stage(override: Optional[int], esp: Optional[int]) -> Tuple[int, str]:
    """
    Resolve the effective stage from the available sources.

    Args:
        override: manual override, or None for auto.
        esp: live ESP stage, or None when unavailable.

    Returns:
        (stage, source) where source is "manual", "auto", or "default".
    """
    if override is not None:
        return override, "manual"
    if esp is not None:
        return esp, "auto"
    return 0, "default"


def get_effective_stage(manual_override: Optional[int] = None) -> Tuple[int, str]:
    """
    Return the effective load shedding stage and where it came from.

    Args:
        manual_override: Optional manual stage override (0-8), or None for auto.

    Returns:
        Tuple of (stage: int, source: str)
        - stage: 0-8
        - source: "manual", "auto" (ESP API), or "default"

    Resolution order:
        1. Manual override if provided
        2. ESP API if no override
        3. Default to stage 0
    """
    esp = fetch_esp_stage() if manual_override is None else None
    return _resolve_stage(manual_override, esp)


# ---------------------------------------------------------------------------
# UI Helpers
# ---------------------------------------------------------------------------

def stage_color(stage: int) -> Tuple[str, str]:
    """
    Return the (background, foreground) badge colors for a stage.

    Bands: 0 green, 1-2 amber, 3-4 red, 5+ dark red. Out-of-range values
    fall back to the severe band so a bad value is never understated.
    """
    if stage == 0:
        return STAGE_COLORS["green"]
    if 1 <= stage <= 2:
        return STAGE_COLORS["amber"]
    if 3 <= stage <= 4:
        return STAGE_COLORS["red"]
    return STAGE_COLORS["dark_red"]


def stage_display_info(stage: int, source: str) -> dict:
    """
    Get display information for a load shedding stage.

    Returns:
        Dict with keys: level, source, bg_color, fg_color, label
    """
    bg_color, fg_color = stage_color(stage)

    return {
        "level": stage,
        "source": source,
        "bg_color": bg_color,
        "fg_color": fg_color,
        "label": f"Stage {stage}",
        "css_class": f"stage-{stage}",
    }
