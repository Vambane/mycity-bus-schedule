"""
loadshedding.py — Eskom load shedding stage awareness
======================================================
Answers exactly one question for the rest of the app: what stage is it?

Resolution order:
    1. Manual sidebar override (session key "ls_stage_override")
    2. Live stage from the EskomSePush (ESP) API
    3. Default: stage 0 (no load shedding assumed)

The ESP status endpoint needs an API key, read from
st.secrets["ESP_API_KEY"] with an ESP_API_KEY environment variable
fallback. Without a key, or on any network or parse failure, the fetch
degrades to None (never raises), and the app assumes stage 0.

This module knows nothing about stops, blocks, or journeys.
"""

import logging
import os

import requests
import streamlit as st

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ESP_STATUS_URL = "https://developer.sepush.co.za/business/2.0/status"
REQUEST_TIMEOUT_S = 5

# Session key for the sidebar override selectbox ("Auto" or "0".."8")
OVERRIDE_KEY = "ls_stage_override"

GOLD = "#D4AF37"

# Badge colors per stage band: (background, foreground)
_STAGE_COLORS: dict[str, tuple[str, str]] = {
    "green": ("#e6f4ea", "#137333"),      # stage 0: no load shedding
    "amber": ("#fef7e0", "#b45309"),      # stages 1-2: mild
    "red": ("#fce8e6", "#c5221f"),        # stages 3-4: serious
    "dark_red": ("#f3d6d6", "#8b0000"),   # stages 5+: severe
}


# ---------------------------------------------------------------------------
# Stage source
# ---------------------------------------------------------------------------

def _get_api_key() -> str | None:
    """Return the ESP API key from Streamlit secrets or the environment.

    st.secrets raises when no secrets.toml exists at all, so the lookup is
    wrapped broadly; a missing key simply disables the live source.
    """
    try:
        key = st.secrets["ESP_API_KEY"]
        if key:
            return str(key)
    except Exception:  # no secrets.toml, or key absent — fall through to env
        pass
    return os.environ.get("ESP_API_KEY") or None


def _fetch_esp_stage_uncached() -> int | None:
    """Fetch the current national Eskom stage from the ESP API.

    Returns:
        The stage as an int in 0..8, or None on any failure: missing API
        key, network error, HTTP error, malformed payload, or an
        out-of-range stage value. Never raises.
    """
    key = _get_api_key()
    if key is None:
        return None
    try:
        resp = requests.get(
            ESP_STATUS_URL,
            headers={"token": key},
            timeout=REQUEST_TIMEOUT_S,
        )
        resp.raise_for_status()
        stage = int(resp.json()["status"]["eskom"]["stage"])
    except Exception as exc:  # any failure degrades to "unknown" (None)
        log.warning("ESP stage fetch failed: %s", exc)
        return None
    if not 0 <= stage <= 8:
        log.warning("ESP returned out-of-range stage %s; ignoring", stage)
        return None
    return stage


# 30 min TTL keeps a full day at 48 calls, under ESP's free tier of 50/day.
@st.cache_data(ttl=1800, show_spinner=False)
def fetch_esp_stage() -> int | None:
    """Cached wrapper around the ESP stage fetch (see _fetch_esp_stage_uncached)."""
    return _fetch_esp_stage_uncached()


# ---------------------------------------------------------------------------
# Stage resolution
# ---------------------------------------------------------------------------

def _resolve_stage(override: int | None, esp: int | None) -> tuple[int, str]:
    """Resolve the effective stage from the available sources.

    Args:
        override: manual sidebar override, or None for "Auto".
        esp: live ESP stage, or None when unavailable.

    Returns:
        (stage, source) where source is "manual", "esp", or "default".
    """
    if override is not None:
        return override, "manual"
    if esp is not None:
        return esp, "esp"
    return 0, "default"


def get_effective_stage() -> tuple[int, str]:
    """Return the effective load shedding stage and where it came from.

    Resolution order: manual sidebar override, then the ESP API, then a
    stage 0 default. The ESP API is only consulted when no override is set,
    so a manual override costs no API calls.
    """
    raw = st.session_state.get(OVERRIDE_KEY)
    override = int(raw) if raw is not None and raw != "Auto" else None
    esp = fetch_esp_stage() if override is None else None
    return _resolve_stage(override, esp)


# ---------------------------------------------------------------------------
# Sidebar UI
# ---------------------------------------------------------------------------

def _stage_color(stage: int) -> tuple[str, str]:
    """Return the (background, foreground) badge colors for a stage.

    Bands: 0 green, 1-2 amber, 3-4 red, 5+ dark red. Out-of-range values
    fall back to the severe band so a bad value is never understated.
    """
    if stage == 0:
        return _STAGE_COLORS["green"]
    if 1 <= stage <= 2:
        return _STAGE_COLORS["amber"]
    if 3 <= stage <= 4:
        return _STAGE_COLORS["red"]
    return _STAGE_COLORS["dark_red"]


_SOURCE_LABELS = {
    "manual": "manual override",
    "esp": "live · EskomSePush",
    "default": "assumed · no API key",
}


def render_stage_sidebar() -> int:
    """Render the load shedding panel in the sidebar and return the stage.

    Shows a gold-accented header, an override selectbox (Auto plus stages
    0 to 8), and a colored stage badge with its source underneath.
    """
    st.sidebar.markdown(
        f'<div style="color:{GOLD};font-weight:700;font-size:1.05rem;'
        f'margin-bottom:0.25rem">⚡ Load shedding</div>',
        unsafe_allow_html=True,
    )
    st.sidebar.selectbox(
        "Stage override",
        ["Auto"] + [str(i) for i in range(9)],
        key=OVERRIDE_KEY,
        help="Auto uses the live EskomSePush stage when an API key is set.",
    )

    stage, source = get_effective_stage()
    bg, fg = _stage_color(stage)
    st.sidebar.markdown(
        f'<span style="background:{bg};color:{fg};padding:2px 10px;'
        f'border-radius:12px;font-size:0.78rem;font-weight:600">'
        f"Stage {stage}</span>",
        unsafe_allow_html=True,
    )
    st.sidebar.caption(_SOURCE_LABELS.get(source, source))
    return stage
