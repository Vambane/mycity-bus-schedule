"""One brand across the landing page and the app pages.

The landing (/) and the app (/search, /journey, /stop, /map) share
tokens.css: same fonts, palette, borders and shadows. These tests
stop the two from drifting apart again (e.g. a second accent colour
creeping back into the app stylesheets).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fastapi_app.main import app

CSS = Path("fastapi_app/static/css")
APP_SHEETS = ("tokens.css", "base.css", "components.css")
BRAND_YELLOW = "#ffe17c"
OLD_LIME = "#c8ff00"


@pytest.fixture(name="client", scope="module")
def fixture_client():
    """One app client for the module (starts the app once)."""
    with TestClient(app) as c:
        yield c


def _hex_to_rgb(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))


def _luminance(h: str) -> float:
    """WCAG relative luminance of a #RRGGBB colour."""
    def ch(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (ch(c) for c in _hex_to_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(fg: str, bg: str) -> float:
    """WCAG 2.x contrast ratio between two #RRGGBB colours."""
    a, b = sorted((_luminance(fg), _luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


@pytest.mark.parametrize("path", ["/", "/search", "/map", "/stop/Civic Centre"])
def test_every_page_loads_shared_tokens(client, path):
    """Landing and app pages all link the one tokens file."""
    html = client.get(path).text
    assert "/static/css/tokens.css" in html


def test_tokens_hold_brand_fonts_and_palette():
    """Fonts and the yellow live in tokens.css, self-hosted."""
    tokens = (CSS / "tokens.css").read_text().lower()
    assert "cabinetgrotesk-extrabold.woff2" in tokens
    assert "satoshi-medium.woff2" in tokens
    assert BRAND_YELLOW in tokens
    # Self-hosted only: no third-party font CDN for app or landing.
    assert "fonts.googleapis.com" not in tokens
    assert "fontshare.com" not in tokens


def test_no_lime_left_in_app_or_map():
    """The retired lime accent is gone from app styles and the map."""
    sheets = [CSS / name for name in APP_SHEETS]
    sheets.append(Path("fastapi_app/static/map/system_map.html"))
    for sheet in sheets:
        text = sheet.read_text().lower()
        assert OLD_LIME not in text, f"old lime accent in {sheet}"
        assert "var(--lime" not in text, f"--lime token used in {sheet}"


def test_app_header_uses_landing_logo_mark(client):
    """App header carries the landing's bolt mark, not the bus emoji."""
    html = client.get("/search").text
    assert 'class="logo-mark"' in html
    assert "&#x1F68D;" not in html  # old bus emoji logo
    assert 'aria-label="Halte home"' in html


def test_landing_does_not_redefine_brand_values():
    """landing.css aliases the shared tokens instead of copying them."""
    landing = (CSS / "landing.css").read_text().lower()
    assert "@font-face" not in landing
    assert BRAND_YELLOW not in landing


@pytest.mark.parametrize(
    "fg,bg,minimum",
    [
        ("#000000", "#FFE17C", 4.5),  # ink on yellow (buttons, alerts, hero)
        ("#FFE17C", "#000000", 4.5),  # yellow on ink (active pill, primary btn)
        ("#FFFFFF", "#171E19", 4.5),  # times on the charcoal strip
        ("#B7C6C2", "#171E19", 4.5),  # stop names on the charcoal strip
        ("#000000", "#B7C6C2", 4.5),  # ink on sage (info alert, transfer badge)
        ("#555C57", "#FFFFFF", 4.5),  # tertiary text on paper
        ("#555C57", "#F4F4F4", 4.5),  # tertiary text in the trust strip
        ("#064E30", "#D7F0E2", 4.5),  # stage 0 badge
        ("#8A1010", "#FFD9D6", 4.5),  # critical stage badge / danger alert
    ],
)
def test_brand_pairs_meet_wcag_aa(fg, bg, minimum):
    """Every text/fill pair the brand uses passes WCAG AA."""
    assert contrast(fg, bg) >= minimum, f"{fg} on {bg}: {contrast(fg, bg):.2f}"


def test_contrast_comments_in_tokens_are_honest():
    """Ratios written next to text tokens must match the real maths."""
    tokens = (CSS / "tokens.css").read_text()
    pattern = (
        r"--(text-secondary|text-tertiary|ls-ok-fg|ls-crit-fg):"
        r"\s*(#[0-9A-Fa-f]{6});\s*/\*\s*([\d.]+):1"
    )
    for name, hex_, ratio in re.findall(pattern, tokens):
        bg = {"ls-ok-fg": "#D7F0E2", "ls-crit-fg": "#FFD9D6"}.get(name, "#FFFFFF")
        assert abs(contrast(hex_, bg) - float(ratio)) < 0.15, name
