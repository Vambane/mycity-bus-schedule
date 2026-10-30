"""Trips that run past midnight.

A trip printed as 23:50, 00:05 is stored as 23:50, 24:05 on its service
day so its stops stay in order. These tests cover the scraper rewrite,
the display helper, and the two pages, including the window just after
midnight when yesterday's late trips are still running.

The real snapshot may not contain an overnight trip, so the page tests
add a made-up one ("Night Owl", route N99) to a private copy of the
database and point the app at it.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest
from fastapi.testclient import TestClient

import fastapi_app.routes.search as search_route
import fastapi_app.routes.stop as stop_route
from etl.scrape_uct import crosses_midnight, unroll_midnight
from fastapi_app.dependencies import get_connection
from fastapi_app.main import app
from journey import clock_time

CPT = ZoneInfo("Africa/Johannesburg")
SNAPSHOT = Path(__file__).resolve().parents[1] / "data" / "snapshot"

A, B = "Night Owl Library", "Night Owl Res"


# ---------------------------------------------------------------------------
# Scraper and display helpers
# ---------------------------------------------------------------------------

def test_unroll_midnight_moves_after_midnight_times_past_24():
    row = ["23:15:00", None, "23:50:00", "00:05:00", "00:20:00"]
    assert crosses_midnight(row)
    assert unroll_midnight(row) == ["23:15:00", None, "23:50:00", "24:05:00", "24:20:00"]


def test_unroll_midnight_leaves_ordinary_trips_alone():
    row = ["07:00:00", "07:10:00", None, "07:30:00"]
    assert not crosses_midnight(row)
    assert unroll_midnight(row) == row


def test_small_backward_step_is_not_midnight():
    # A misprint (12:55 then 12:50) must not become a next-day time.
    assert not crosses_midnight(["12:55:00", "12:50:00"])


@pytest.mark.parametrize(
    "stored,shown",
    [("24:05:00", "00:05"), ("24:05", "00:05"), ("07:05:00", "07:05"),
     ("23:59:00", "23:59"), ("", ""), (None, "")],
)
def test_clock_time(stored, shown):
    assert clock_time(stored) == shown


# ---------------------------------------------------------------------------
# Pages, against a private database with one overnight route
# ---------------------------------------------------------------------------

def _night_owl_db(path: Path) -> duckdb.DuckDBPyConnection:
    """Snapshot copy plus route N99: weekday trips across midnight, a Saturday morning trip."""
    con = duckdb.connect(str(path))
    for pq in sorted(SNAPSHOT.glob("*.parquet")):
        con.execute(f"CREATE TABLE {pq.stem} AS SELECT * FROM read_parquet(?)", [str(pq)])
    con.execute(
        "INSERT INTO routes BY NAME SELECT 'N99' AS route_id, "
        "'N99 Night Owl' AS route_name, 'myciti' AS operator"
    )
    trips = [  # (day_type, time at A, time at B)
        ("weekday", "23:15:00", "23:40:00"),
        ("weekday", "23:50:00", "24:05:00"),  # crosses midnight on the way
        ("weekday", "24:15:00", "24:40:00"),  # leaves after midnight
        ("saturday", "06:00:00", "06:25:00"),
    ]
    for day_type, t_a, t_b in trips:
        for stop, t in ((A, t_a), (B, t_b)):
            con.execute(
                "INSERT INTO departures BY NAME SELECT 'N99' AS route_id, ? AS stop_name, "
                "'To Night Owl Res' AS direction, ? AS day_type, ? AS departure_time, "
                "'myciti' AS operator",
                [stop, day_type, t],
            )
    con.close()
    return duckdb.connect(str(path), read_only=True)


@pytest.fixture(name="client", scope="module")
def fixture_client(tmp_path_factory):
    con = _night_owl_db(tmp_path_factory.mktemp("db") / "night_owl.duckdb")
    app.dependency_overrides[get_connection] = lambda: con
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_connection, None)
        con.close()


@pytest.fixture(name="at")
def fixture_at(monkeypatch):
    """Freeze both routes' clock at a given Cape Town time."""
    def freeze(when: datetime) -> None:
        monkeypatch.setattr(stop_route, "get_cape_town_time", lambda: when)
        monkeypatch.setattr(search_route, "get_cape_town_time", lambda: when)
    return freeze


def _search(client, **params) -> str:
    return client.get("/search", params={"from_stop": A, "to_stop": B, **params}).text


def test_search_shows_clock_times_not_24(client, at):
    at(datetime(2026, 10, 28, 23, 0, tzinfo=CPT))  # Wednesday night
    html = _search(client)
    assert "23:50" in html and "00:05" in html
    assert ">24:05<" not in html
    # Links keep the stored time, so the journey page finds the trip.
    assert "arr=24%3A05" in html


def test_search_just_after_midnight_includes_yesterdays_trip(client, at):
    # Saturday 00:10: the 00:15 bus is Friday's weekday trip (stored 24:15).
    at(datetime(2026, 10, 31, 0, 10, tzinfo=CPT))
    html = _search(client)
    assert "00:15" in html and "06:00" in html
    assert html.index("00:15") < html.index("06:00")  # in real time order
    assert re.search(r"day_type=weekday[^\"]*dep=24%3A15", html)


def test_search_carryover_stops_in_the_morning(client, at):
    at(datetime(2026, 10, 31, 7, 0, tzinfo=CPT))  # Saturday 07:00
    html = _search(client)
    assert "00:15" not in html


def test_stop_board_just_after_midnight(client, at):
    at(datetime(2026, 10, 31, 0, 10, tzinfo=CPT))
    html = client.get(f"/stop/{A}").text
    assert "00:15" in html and "06:00" in html
    assert html.index("00:15") < html.index("06:00")
    assert ">24:15<" not in html


def test_journey_page_shows_clock_times(client, at):
    at(datetime(2026, 10, 28, 23, 0, tzinfo=CPT))
    html = client.get("/journey", params={
        "from_stop": A, "to_stop": B, "day_type": "weekday", "route_id": "N99",
        "direction": "To Night Owl Res", "dep": "23:50", "arr": "24:05",
    }).text
    assert "00:05" in html
    assert "24:05" not in html
