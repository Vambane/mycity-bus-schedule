"""Neo-Brutalist landing page at `/`; search at `/search`."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from fastapi_app.main import app

DISCLAIMER = (
    "Halte is an independent app, not affiliated with the City of Cape Town, "
    "MyCiTi or the University of Cape Town."
)

BANNED = (
    "real-time",
    "realtime",
    "track your bus",
    "never miss",
    "testimonial",
    "★",
    "⭐",
    "user count",
    "users love",
    "works offline",
    "offline mode",
)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_landing_renders_at_root(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert 'class="landing"' in html
    assert "Your bus," in html
    assert "sorted" in html
    assert "Plan a trip" in html
    assert 'href="/search"' in html
    assert "landing.css" in html
    assert "CabinetGrotesk-Extrabold.woff2" in open(
        "fastapi_app/static/css/landing.css"
    ).read() or "CabinetGrotesk" in open(
        "fastapi_app/static/css/landing.css"
    ).read()


def test_search_works_at_search_path(client):
    resp = client.get("/search")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="search-form"' in html
    assert "From" in html and "To" in html
    assert 'action="/search"' in html


def test_root_with_query_301_to_search(client):
    # TestClient follows redirects by default — disable for this check.
    resp = client.get(
        "/",
        params={"from_stop": "Civic Centre", "to_stop": "Camps Bay"},
        follow_redirects=False,
    )
    assert resp.status_code == 301
    loc = resp.headers["location"]
    assert loc.startswith("/search?")
    assert "from_stop=Civic" in loc or "from_stop=Civic%20Centre" in loc
    assert "to_stop=" in loc
    # Relative path only — never an absolute URL from a parameter.
    assert not loc.startswith("http")
    assert "://" not in loc


def test_footer_disclaimer_and_honesty(client):
    html = client.get("/").text
    assert DISCLAIMER in html
    assert "Planning aid only" in html
    assert "Estimated from timetable — not live GPS." in html
    assert "OpenStreetMap" in html
    assert "© OpenStreetMap contributors" in html or "OpenStreetMap contributors" in html


def test_landing_no_banned_words(client):
    html = client.get("/").text.lower()
    for word in BANNED:
        assert word not in html, f"banned phrase present: {word}"
    # "live" alone appears in "not live GPS" / "not a live Eskom" — allowed context.
    # Ban standalone marketing "live tracking" style claims:
    assert "live gps" not in html.replace("not live gps", "")
    assert "live tracking" not in html
    assert re.search(r"\bai\b", html) is None
    assert "works offline" not in html


def test_landing_no_fontshare_cdn(client):
    html = client.get("/").text.lower()
    assert "fontshare.com" not in html
    assert "cdn.fontshare" not in html
    css = open("fastapi_app/static/css/landing.css").read().lower()
    assert "fontshare.com" not in css
    assert "fonts.googleapis.com" not in css


def test_marquee_from_real_routes(client):
    html = client.get("/").text
    assert "T01" in html or "Dunoon" in html
    assert "landing-marquee" in html
    assert "aria-hidden=\"true\"" in html
    # No |safe dump of route HTML
    assert "|safe" not in open("fastapi_app/templates/pages/landing.html").read()


def test_load_shedding_card_copy(client):
    html = client.get("/").text
    assert (
        "Shows the load-shedding stage we're assuming: Stage 0 by default, "
        "not a live Eskom feed. Check before you travel."
    ) in html


def test_hero_subhead_says_plan_your_trip(client):
    html = client.get("/").text
    assert "plan your trip" in html
    assert "plan your connections" not in html


def test_self_hosted_fonts_and_licence():
    from pathlib import Path

    fonts = Path("fastapi_app/static/fonts")
    assert (fonts / "CabinetGrotesk-Extrabold.woff2").is_file()
    assert (fonts / "Satoshi-Medium.woff2").is_file()
    note = Path("FONTSHARE_LICENCE.md").read_text()
    assert "Fontshare" in note
    assert "Cabinet Grotesk" in note
    assert "Satoshi" in note


def test_hero_mockup_image_exists(client):
    from pathlib import Path

    assert Path("fastapi_app/static/img/landing-search.webp").is_file()
    html = client.get("/").text
    assert "landing-search.webp" in html
    assert 'alt="Halte journey search showing departures"' in html
    # Hero crop must show the phone short-form badge exactly as the app renders it.
    assert 'data-stage-badge="Stage 0 · default"' in html
    img = client.get("/static/img/landing-search.webp")
    assert img.status_code == 200
    assert img.content[:4] == b"RIFF" or img.headers["content-type"].startswith("image/")
