"""The map page when it opens for one trip preview.

Kept apart from test_trip_preview.py, whose read-only database connection
cannot share the file with the app's own connections.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    from fastapi_app.main import app

    with TestClient(app) as c:
        yield c


def test_map_page_leads_with_the_trip_when_previewing(client):
    resp = client.get(
        "/map?preview=1&route_id=T01&direction=To%20T01%20Dunoon&day_type=weekday"
        "&board=Milnerton&alight=Table%20View&dep=16%3A38"
    )
    assert resp.status_code == 200
    html = resp.text
    assert "Trip preview" in html
    assert 'loading="eager"' in html
    assert html.index("map-frame") < html.index("trust-strip")


def test_network_map_unchanged_without_preview(client):
    html = client.get("/map").text
    assert "Network Map" in html
    assert 'loading="lazy"' in html
