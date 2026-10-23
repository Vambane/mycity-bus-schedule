"""Journey schematic page — validation, rebuild, generic errors."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import duckdb
import pytest
from fastapi.testclient import TestClient

from fastapi_app.services.journey_page import (
    JourneyParamError,
    build_journey_view,
    decode_legs,
    encode_legs,
    journey_href_direct,
    search_back_href,
)

DB = Path(__file__).resolve().parents[1] / "data" / "myciti.duckdb"


def _open_ro():
    if not DB.exists():
        pytest.skip("myciti.duckdb missing")
    return duckdb.connect(str(DB), read_only=True)


def _sample_direct():
    con = _open_ro()
    try:
        row = con.execute(
            """
            SELECT d.route_id, d.direction, d.day_type
            FROM departures d
            WHERE d.day_type = 'saturday'
            LIMIT 1
            """
        ).fetchone()
        route_id, direction, day_type = row
        stops = [
            r[0]
            for r in con.execute(
                """
                SELECT stop_name FROM departures
                WHERE route_id = ? AND direction = ? AND day_type = ?
                GROUP BY stop_name ORDER BY MIN(departure_time)
                """,
                [route_id, direction, day_type],
            ).fetchall()
        ]
        board, alight = stops[0], stops[min(4, len(stops) - 1)]
        dep = con.execute(
            """
            SELECT departure_time FROM departures
            WHERE route_id = ? AND direction = ? AND day_type = ? AND stop_name = ?
            ORDER BY departure_time LIMIT 1
            """,
            [route_id, direction, day_type, board],
        ).fetchone()[0]
        arr = con.execute(
            """
            SELECT departure_time FROM departures
            WHERE route_id = ? AND direction = ? AND day_type = ? AND stop_name = ?
            ORDER BY departure_time LIMIT 1
            """,
            [route_id, direction, day_type, alight],
        ).fetchone()[0]
        return {
            "route_id": route_id,
            "direction": direction,
            "day_type": day_type,
            "from_stop": board,
            "to_stop": alight,
            "dep": str(dep)[:5],
            "arr": str(arr)[:5],
        }
    finally:
        con.close()


@pytest.fixture(scope="module")
def client():
    from fastapi_app.main import app

    with TestClient(app) as c:
        yield c


def test_back_href_is_relative_search_only():
    href = search_back_href(
        from_stop="Civic Centre",
        to_stop="Camps Bay",
        day_type="saturday",
        operator="both",
    )
    assert href.startswith("/?")
    assert "://" not in href
    qs = parse_qs(urlparse(href).query)
    assert qs["from_stop"] == ["Civic Centre"]
    assert qs["day_type"] == ["saturday"]


def test_reject_bad_day_type():
    con = _open_ro()
    try:
        with pytest.raises(JourneyParamError):
            build_journey_view(
                con,
                from_stop="Civic Centre",
                to_stop="Camps Bay",
                day_type="monday",
                route_id="105",
                direction="To somewhere",
                dep="08:00",
                arr="08:30",
            )
    finally:
        con.close()


def test_reject_path_injection_in_stop():
    con = _open_ro()
    try:
        with pytest.raises(JourneyParamError):
            build_journey_view(
                con,
                from_stop="../etc/passwd",
                to_stop="Camps Bay",
                day_type="saturday",
                route_id="105",
                direction="To somewhere",
                dep="08:00",
                arr="08:30",
            )
    finally:
        con.close()


def test_rebuild_direct_lists_every_stop():
    sample = _sample_direct()
    con = _open_ro()
    try:
        view = build_journey_view(con, **sample)
        assert view["kind"] == "direct"
        assert len(view["stops"]) >= 2
        assert view["stops"][0] == sample["from_stop"]
        assert view["stops"][-1] == sample["to_stop"]
        assert view["back_href"].startswith("/?")
    finally:
        con.close()


def test_http_journey_ok(client):
    sample = _sample_direct()
    href = journey_href_direct(**sample)
    resp = client.get(href)
    assert resp.status_code == 200, resp.text
    assert sample["from_stop"] in resp.text
    assert sample["to_stop"] in resp.text
    assert "journey-stops-schematic--page" in resp.text
    assert "Search results" in resp.text
    assert "/workspace" not in resp.text
    assert "traceback" not in resp.text.lower()


def test_http_bad_params_generic_400(client):
    resp = client.get(
        "/journey",
        params={
            "from_stop": "A",
            "to_stop": "B",
            "day_type": "notaday",
            "route_id": "105",
            "direction": "X",
            "dep": "08:00",
            "arr": "09:00",
        },
    )
    assert resp.status_code == 400
    assert "invalid or incomplete" in resp.text.lower()
    assert "JourneyParamError" not in resp.text
    assert "Traceback" not in resp.text


def test_http_unknown_journey_generic(client):
    resp = client.get(
        "/journey",
        params={
            "from_stop": "Civic Centre",
            "to_stop": "Definitely Not A Real Stop ZZ",
            "day_type": "saturday",
            "route_id": "105",
            "direction": "To 105 Sea Point",
            "dep": "08:00",
            "arr": "09:00",
        },
    )
    assert resp.status_code in (400, 404)
    body = resp.text.lower()
    assert "could not rebuild" in body or "invalid or incomplete" in body
    assert "leg_stop_sequence" not in resp.text


def test_encode_legs_roundtrip():
    legs = [
        {
            "route_id": "216",
            "direction": "To Civic",
            "board": "Camps Bay",
            "alight": "Civic Centre",
            "dep": "08:00",
            "arr": "08:25",
        }
    ]
    token = encode_legs(legs)
    out = decode_legs(token)
    assert out[0]["route_id"] == "216"
    assert out[0]["board"] == "Camps Bay"
