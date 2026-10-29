"""'Use my location': stop positions API, and the button in a real browser.

The browser tests fake the rider's position (Playwright geolocation)
and also check the privacy promise: the position never appears in any
request the page makes. They skip themselves when no browser is
installed, like tests/test_journey_card_hit.py.
"""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fastapi_app.main import app

REPO = Path(__file__).resolve().parents[1]

# A spot on the pavement about 60 m east of the Civic Centre stop.
NEAR_CIVIC_CENTRE = {"latitude": -33.92070, "longitude": 18.43030}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.fixture(name="client", scope="module")
def fixture_client():
    with TestClient(app) as c:
        yield c


def test_locations_cover_myciti_stops_in_cape_town(client):
    data = client.get("/api/stops/locations").json()
    stops = {s["name"]: s for s in data["stops"]}
    assert len(stops) > 400
    assert stops["Civic Centre"]["lat"] == pytest.approx(-33.9206, abs=0.001)
    for s in stops.values():  # all inside greater Cape Town
        assert -34.4 < s["lat"] < -33.4 and 18.2 < s["lon"] < 19.0
    assert data["unmapped"] >= 0


def test_locations_only_for_searchable_names(client):
    names = set(client.get("/api/stops").json()["stops"])
    located = {s["name"] for s in client.get("/api/stops/locations").json()["stops"]}
    assert located <= names


def test_uct_filter_gets_no_borrowed_positions(client):
    # "Sandown" is a UCT stop and a MyCiTi stop in a different suburb:
    # UCT-only must not borrow the MyCiTi position.
    data = client.get("/api/stops/locations", params={"operator": "uct"}).json()
    assert data["stops"] == []
    assert data["unmapped"] > 0


def test_bad_operator_is_rejected(client):
    assert client.get("/api/stops/locations", params={"operator": "taxi"}).status_code == 422


def test_button_and_privacy_line_are_on_the_form(client):
    html = client.get("/search").text
    assert 'id="locate-btn"' in html and "hidden" in html  # JS reveals it
    assert "Location stays on your device" in html
    assert "nearest.js" in html


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(name="server", scope="module")
def fixture_server():
    port = _free_port()
    with subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "fastapi_app.main:app", "--port", str(port)],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ) as proc:
        for _ in range(80):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                    break
            except OSError:
                time.sleep(0.25)
        yield f"http://127.0.0.1:{port}"
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(name="browser", scope="module")
def fixture_browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # no browser installed
            pytest.skip(f"no browser: {exc}")
        yield b
        b.close()


def _page(browser, server, *, allow=True):
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        geolocation=NEAR_CIVIC_CENTRE,
        permissions=["geolocation"] if allow else [],
    )
    page = ctx.new_page()
    urls: list[str] = []
    page.on("request", lambda r: urls.append(r.url))
    page.goto(f"{server}/search", wait_until="networkidle")
    return ctx, page, urls


def test_use_my_location_fills_from_with_nearest_stop(browser, server):
    ctx, page, urls = _page(browser, server)
    try:
        assert page.is_visible("#locate-btn")
        assert not page.is_visible("#locate-privacy")
        page.click("#locate-btn")
        page.wait_for_selector("#locate-status .locate-chips")

        assert page.input_value('[data-combobox="from_stop"] .combobox-input') == "Civic Centre"
        assert page.input_value('select[name="from_stop"]') == "Civic Centre"
        assert "Nearest stop: Civic Centre" in page.inner_text("#locate-status")
        assert page.is_visible("#locate-privacy")

        # Choosing an alternative chip switches From
        chip = page.locator("#locate-status .recent-chip").first
        other = chip.inner_text().split(" · ")[0]
        chip.click()
        assert page.input_value('select[name="from_stop"]') == other

        # Privacy: the position is never sent anywhere
        assert not any("33.920" in u or "18.430" in u for u in urls)
    finally:
        ctx.close()


def test_blocked_location_explains_what_to_do(browser, server):
    ctx, page, _ = _page(browser, server, allow=False)
    try:
        page.click("#locate-btn")
        page.wait_for_function(
            "document.getElementById('locate-status').textContent.includes('blocked')"
        )
        assert page.input_value('select[name="from_stop"]') == ""
    finally:
        ctx.close()
