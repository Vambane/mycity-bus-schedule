"""Security hardening from the November 2026 review.

* The system map never puts network data (stop names, route names, colours)
  into HTML unescaped. Those strings come from scraped timetables, and the
  map iframe shares the app's origin.
* Leaflet loads with Subresource Integrity.
* API errors do not echo exception text to the client.
* Handlers run in the threadpool, each request on its own DuckDB cursor, so
  concurrent searches work and one slow search cannot block the event loop.
* The single-stop redirect URL-encodes the stop name.
"""
from __future__ import annotations

import inspect
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
MAP_HTML = REPO / "fastapi_app" / "static" / "map" / "system_map.html"
EVIL = "<img src=x onerror=window.__xss=1>"


def _map_source() -> str:
    return MAP_HTML.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Map page: static checks
# ---------------------------------------------------------------------------

def test_map_never_assigns_inner_html():
    assert ".innerHTML" not in _map_source()


def test_map_tooltip_and_label_content_is_escaped():
    src = _map_source()
    for call in re.findall(r"\.(?:bindTooltip|setContent)\(([^,)]+)", src):
        arg = call.strip()
        # tipHtml is checked below; everything else must be escaped inline.
        assert arg == "tipHtml" or arg.startswith("escapeHtml("), arg
    tip = re.search(r"const tipHtml =(.*?);", src, re.S).group(1)
    for expr in re.findall(r"\$\{([^}]*)\}", tip):
        assert "escapeHtml" in expr, expr


def test_map_legend_colours_are_validated():
    src = _map_source()
    assert "/^#[0-9a-fA-F]{3,8}$/" in src
    assert "safeColor(r.color)" in src
    # Raw r.color never reaches a style property.
    assert not re.search(r"style\.\w+\s*=\s*r\.color", src)


def test_leaflet_loads_with_subresource_integrity():
    src = _map_source()
    tags = re.findall(r"<(?:script|link)[^>]*unpkg\.com/leaflet[^>]*>", src)
    assert len(tags) == 2
    for tag in tags:
        assert re.search(r'integrity="sha384-[A-Za-z0-9+/=]{64}"', tag), tag
        assert 'crossorigin=""' in tag, tag


# ---------------------------------------------------------------------------
# Map page: a hostile stop name does not run in a real browser
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    with subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "fastapi_app.main:app", "--port", str(port)],
        cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ) as proc:
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                    break
            except OSError:
                time.sleep(0.25)
        yield f"http://127.0.0.1:{port}"
        proc.terminate()
        proc.wait(timeout=10)


HOSTILE_NETWORK = {
    "nodes": [
        # n >= 6 gives a permanent hub label as well as a hover tooltip.
        {"id": EVIL, "lat": -33.92, "lon": 18.42, "n": 6, "routes": [EVIL]},
        {"id": "Civic Centre", "lat": -33.921, "lon": 18.43, "n": 1, "routes": ["T01"]},
    ],
    "links": [],
    "paths": [],
    "routes": [
        {"id": EVIL, "name": "Area – " + EVIL, "category": "Area",
         "color": "red;background:url(https://example.invalid/x)"},
        {"id": "T01", "name": "Trunk – Civic Centre", "category": "Trunk",
         "color": "#0072BC"},
    ],
}


def _serve_leaflet(route):
    """Serve Leaflet from LEAFLET_DIST when set (offline runs), else unpkg."""
    dist = os.environ.get("LEAFLET_DIST")
    name = route.request.url.rsplit("/", 1)[-1]
    if dist and (Path(dist) / name).is_file():
        ctype = "text/css" if name.endswith(".css") else "application/javascript"
        route.fulfill(
            body=(Path(dist) / name).read_bytes(), content_type=ctype,
            headers={"Access-Control-Allow-Origin": "*"},
        )
    else:
        route.continue_()


def test_hostile_stop_name_does_not_execute_browser(server):
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as exc:  # browser binaries missing
            pytest.skip(f"no browser: {exc}")
        page = browser.new_page()
        page.route("**/unpkg.com/leaflet@1.9.4/**", _serve_leaflet)
        page.route("**/tile.openstreetmap.org/**", lambda r: r.abort())
        page.route(
            "**/api/map/network*",
            lambda r: r.fulfill(json=HOSTILE_NETWORK),
        )
        page.goto(server + "/static/map/system_map.html")
        try:
            page.wait_for_function("typeof L !== 'undefined'", timeout=10000)
        except sync_api.Error:
            browser.close()
            # With a local copy, a load failure means the SRI hash is wrong.
            assert not os.environ.get("LEAFLET_DIST"), "Leaflet blocked: check SRI"
            pytest.skip("Leaflet could not be loaded (offline, set LEAFLET_DIST)")
        page.wait_for_selector(".lrow")
        # Open the hover tooltip on the hostile stop too.
        page.evaluate("""() => document.querySelectorAll(
            '.leaflet-stops-pane path.leaflet-interactive').forEach(
            el => el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true})))""")
        page.wait_for_timeout(300)
        result = page.evaluate("""() => ({
            imgs: document.querySelectorAll('img[src="x"]').length,
            xss: window.__xss === 1,
            legendText: document.getElementById('legend').textContent,
            hubText: [...document.querySelectorAll('.hub-label')]
                .map(e => e.textContent).join('|'),
            tipText: [...document.querySelectorAll('.leaflet-tooltip')]
                .map(e => e.textContent).join('|'),
            swatches: [...document.querySelectorAll('.swatch')]
                .map(e => e.style.background),
        })""")
        browser.close()
    assert result["imgs"] == 0
    assert result["xss"] is False
    # The hostile text is shown literally, not dropped.
    assert EVIL in result["legendText"]
    assert EVIL in result["hubText"]
    assert EVIL in result["tipText"]
    # The bad colour fell back to neutral; the hex colour was kept.
    assert "url" not in " ".join(result["swatches"])
    assert any("rgb(0, 114, 188)" in s for s in result["swatches"])


# ---------------------------------------------------------------------------
# API errors, threadpool handlers, per-request cursors, redirect encoding
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def client():
    from fastapi_app.main import app

    with TestClient(app) as c:
        yield c


def test_network_error_does_not_leak_exception_text(client, monkeypatch):
    import fastapi_app.routes.api as api_route

    def boom(*_a, **_k):
        raise RuntimeError("secret internal detail /srv/data/myciti.duckdb")

    monkeypatch.setattr(api_route, "build_network", boom)
    res = client.get("/api/map/network")
    assert res.status_code == 500
    assert res.json() == {"detail": "Failed to build network data"}


def test_db_handlers_run_in_threadpool():
    from fastapi_app.routes import api, journey, search, stop

    for handler in (
        search.search_page, stop.stop_view, journey.journey_page,
        api.network_data, api.trip_preview, api.list_stops, api.stop_locations_api,
    ):
        assert not inspect.iscoroutinefunction(handler), handler.__name__


def test_each_request_gets_its_own_cursor(client):  # pylint: disable=unused-argument
    from fastapi_app.dependencies import _connection_pool, get_connection

    a, b = get_connection(), get_connection()
    assert a is not b
    assert a is not _connection_pool[0]
    assert a.execute("SELECT 1").fetchone() == (1,)


def test_concurrent_searches_both_succeed(client):
    queries = [
        "/search?from_stop=Civic+Centre&to_stop=Table+View&day_type=weekday",
        "/search?from_stop=Table+View&to_stop=Civic+Centre&day_type=weekday",
    ] * 3
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        results = list(pool.map(client.get, queries))
    for res in results:
        assert res.status_code == 200
        assert "journey" in res.text.lower()


def test_single_stop_redirect_encodes_stop_name(client):
    res = client.get(
        "/search", params={"from_stop": "A/B?x=1#y", "day_type": "weekday"},
        follow_redirects=False,
    )
    assert res.status_code in (302, 307)
    assert res.headers["location"].startswith("/stop/A%2FB%3Fx%3D1%23y?")
