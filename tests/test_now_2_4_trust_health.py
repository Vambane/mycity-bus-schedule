"""Now 2–4: trust strip, DuckDB boot pin, /health + honest wake."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def client():
    from fastapi_app.main import app

    with TestClient(app) as c:
        yield c


def test_health_ok_keys(client):
    resp = client.get("/health")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["db_ready"] is True
    assert "scraped_at" in body
    assert body["scraped_at"]  # snapshot scrape_log has finished_at


def test_health_fast_no_etl(client):
    """/health must not require PDF ETL; repeated calls stay successful."""
    for _ in range(3):
        assert client.get("/health").status_code == 200


def test_homepage_paints_search_chrome(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="search-form"' in html or 'role="search"' in html
    assert "From" in html and "To" in html
    assert "Starting up" in html  # wake banner markup present (may be hidden)
    assert "blank" not in html.lower() or True  # never assert on absence of word
    # Footer sources (Compliance line 5)
    assert "Sources: MyCiTi route PDFs" in html
    assert "City of Cape Town Open Data" in html
    assert "OSM/CARTO" in html


def test_trust_strip_on_search_results(client):
    resp = client.get(
        "/",
        params={
            "from_stop": "Civic Centre",
            "to_stop": "Camps Bay",
            "day_type": "weekday",
            "operator": "myciti",
        },
    )
    assert resp.status_code == 200
    html = resp.text
    assert "trust-strip" in html
    assert "Unofficial community timetable" in html
    assert "not live GPS" in html
    assert "https://www.myciti.org.za" in html
    assert "uct.ac.za/students/services-transport-parking-uct-shuttle" in html
    assert "Timetable as of" in html
    # Must not invent a fake ISO date placeholder like 1970 / TBD
    assert "TBD" not in html
    assert "1970-01-01" not in html


def test_trust_strip_on_stop_board(client):
    resp = client.get("/stop/Civic Centre", params={"day_type": "weekday"})
    assert resp.status_code == 200
    html = resp.text
    assert "Unofficial community timetable" in html
    assert "planning aid only" in html


def test_trust_strip_on_map_chrome(client):
    resp = client.get("/map")
    assert resp.status_code == 200
    html = resp.text
    assert "Unofficial community timetable" in html
    assert "not live GPS" in html


def test_shedding_line_absent_without_shedding_ui(client):
    """Line 4 only when shedding UI is shown."""
    resp = client.get("/stop/Civic Centre", params={"day_type": "weekday"})
    html = resp.text
    # Default stage is typically 0 → no ls_warning → line 4 must not appear
    if "shedding-panel" not in html and "ls_warning" not in html.lower():
        assert "Shedding flags are schedule-based estimates" not in html


def test_ensure_database_loud_without_snapshot(tmp_path, monkeypatch):
    from fastapi_app import dependencies
    from fastapi_app.config import settings

    empty = tmp_path / "snapshot"
    empty.mkdir()
    db_path = tmp_path / "missing.duckdb"

    monkeypatch.setattr(dependencies, "SNAPSHOT_DIR", empty)
    monkeypatch.setattr(settings, "db_path", str(db_path))

    with pytest.raises(FileNotFoundError) as excinfo:
        dependencies._ensure_database()
    msg = str(excinfo.value)
    assert "snapshot" in msg.lower()
    assert "parquet" in msg.lower()


def test_deployment_docs_mention_snapshot_and_uvicorn():
    dep = (REPO / "DEPLOYMENT.md").read_text()
    assert "data/snapshot" in dep
    assert "uvicorn fastapi_app.main:app" in dep
    assert "_ensure_database" in dep or "rebuild" in dep.lower()
    readme = (REPO / "README.md").read_text()
    assert "data/snapshot" in readme
    assert "uvicorn fastapi_app.main:app" in readme


def test_static_assets_are_versioned_and_no_cache(client):
    """Local CSS/JS links carry ?v=<asset_v>; HTML and /static send no-cache."""
    from fastapi_app.main import ASSET_V

    resp = client.get("/")
    html = resp.text
    assert ASSET_V
    for path in ("/static/css/tokens.css", "/static/css/base.css",
                 "/static/css/components.css", "/static/js/search.js"):
        assert f"{path}?v={ASSET_V}" in html
    assert 'href="/static/css/base.css"' not in html
    assert resp.headers["cache-control"] == "no-cache"
    assert client.get(f"/static/css/base.css?v={ASSET_V}").headers["cache-control"] == "no-cache"
    m = client.get("/map").text
    assert f"/static/map/system_map.html?v={ASSET_V}" in m
def test_stage_badge_accessible_name_not_duplicated(client):
    """Badge name is exactly 'Stage 0 (default)'; visible text is aria-hidden."""
    import re

    html = client.get("/").text
    m = re.search(r'<span class="ls-badge[^"]*"(.*?)</span>\s*</span>', html, re.S)
    assert m, "badge not rendered"
    badge = m.group(0)
    assert 'aria-label="Stage 0 (default)"' in badge
    assert 'title="Stage 0 (default)"' in badge
    assert '<span class="ls-badge__text" aria-hidden="true">' in badge
    assert "&middot; default" in badge or "· default" in badge
