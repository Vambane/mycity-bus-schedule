"""Leave now, depart at, arrive by.

Unit tests for services/trip_window.py, then the search page with the
Cape Town clock frozen. Expected times come from the real snapshot:
Civic Centre -> Camps Bay on weekdays, where the last trips arriving by
08:00 leave at 07:24 (arrives 07:59) and 07:19 (arrives 08:00 exactly),
and the first bus of the day arrives at 06:20.
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import fastapi_app.routes.search as search_route
from fastapi_app.main import app
from fastapi_app.services.trip_window import (
    ARRIVE,
    DEPART,
    NOW,
    TripWindow,
    arriving_by,
    last_departure_by,
    parse_window,
)

CPT = ZoneInfo("Africa/Johannesburg")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "when,at,expected",
    [
        (None, None, TripWindow()),
        ("now", "08:00", TripWindow()),            # time ignored for "now"
        ("arrive", "08:00", TripWindow(ARRIVE, "08:00:00")),
        ("ARRIVE", " 7:05 ", TripWindow(ARRIVE, "07:05:00")),
        ("depart", "23:59", TripWindow(DEPART, "23:59:00")),
        ("arrive", "", TripWindow()),              # half-filled form: leave now
        ("arrive", "25:00", TripWindow()),
        ("arrive", "8am", TripWindow()),
        ("sideways", "08:00", TripWindow()),
    ],
)
def test_parse_window(when, at, expected):
    assert parse_window(when, at) == expected


def test_window_display_time():
    assert TripWindow(ARRIVE, "08:00:00").at_hhmm == "08:00"
    assert TripWindow(NOW).at_hhmm == ""


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

TRIPS = pd.DataFrame({
    "dep": ["07:00:00", "07:20:00", "07:30:00", "07:45:00"],
    "arr": ["07:40:00", "08:00:00", "08:05:00", "08:20:00"],
})


def test_arriving_by_keeps_trips_in_time_latest_first():
    got = arriving_by(TRIPS, "08:00:00")
    assert list(got["dep"]) == ["07:20:00", "07:00:00"]  # 08:00 exactly counts


def test_arriving_by_drops_buses_that_already_left():
    got = arriving_by(TRIPS, "08:00:00", not_before="07:10:00")
    assert list(got["dep"]) == ["07:20:00"]


def test_last_departure_by_ignores_the_clock():
    assert last_departure_by(TRIPS, "08:00:00") == "07:20:00"
    assert last_departure_by(TRIPS, "07:30:00") is None


def test_empty_input_passes_through():
    empty = TRIPS.iloc[0:0]
    assert arriving_by(empty, "08:00:00").empty
    assert last_departure_by(empty, "08:00:00") is None


# ---------------------------------------------------------------------------
# Search page, clock frozen
# ---------------------------------------------------------------------------

@pytest.fixture(name="client", scope="module")
def fixture_client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(name="at_time")
def fixture_at_time(monkeypatch):
    def freeze(when: datetime) -> None:
        monkeypatch.setattr(search_route, "get_cape_town_time", lambda: when)
    return freeze


def _search(client, **params) -> str:
    base = {"from_stop": "Civic Centre", "to_stop": "Camps Bay", "day_type": "weekday"}
    return client.get("/search", params={**base, **params}).text


def _first_card_times(html: str) -> list[str]:
    return re.findall(r'class="journey-time">(\d\d:\d\d)<', html)[:2]


def test_arrive_by_shows_latest_departure_first(client, at_time):
    at_time(datetime(2026, 10, 28, 6, 0, tzinfo=CPT))  # Wednesday, early
    html = _search(client, when="arrive", at="08:00")
    assert "Arriving by 08:00" in html
    assert _first_card_times(html) == ["07:24", "07:59"]
    assert "08:08" not in html  # the 07:33 bus arrives too late


def test_arrive_by_skips_buses_that_already_left(client, at_time):
    at_time(datetime(2026, 10, 28, 7, 22, tzinfo=CPT))
    html = _search(client, when="arrive", at="08:00")
    assert _first_card_times(html) == ["07:24", "07:59"]
    assert ">07:19<" not in html and ">07:15<" not in html


def test_arrive_by_too_late_says_when_the_last_one_left(client, at_time):
    at_time(datetime(2026, 10, 28, 7, 30, tzinfo=CPT))
    html = _search(client, when="arrive", at="08:00")
    assert "Nothing gets you there by 08:00" in html
    assert "left at 07:24" in html


def test_arrive_before_first_bus(client, at_time):
    at_time(datetime(2026, 10, 28, 5, 0, tzinfo=CPT))
    html = _search(client, when="arrive", at="06:00")
    assert "Nothing gets you there by 06:00" in html
    assert "left at" not in html  # nothing ran yet, so nothing was missed


def test_arrive_by_another_day_is_not_cut_off_by_the_clock(client, at_time):
    at_time(datetime(2026, 10, 28, 21, 0, tzinfo=CPT))  # Wednesday night
    html = _search(client, when="arrive", at="08:00", day_type="saturday")
    assert "Arriving by 08:00" in html
    assert "Nothing gets you there" not in html


def test_depart_at(client, at_time):
    at_time(datetime(2026, 10, 28, 6, 0, tzinfo=CPT))
    html = _search(client, when="depart", at="07:30")
    assert "Departing from 07:30" in html
    assert _first_card_times(html) == ["07:33", "08:08"]


def test_form_remembers_the_choice(client, at_time):
    at_time(datetime(2026, 10, 28, 6, 0, tzinfo=CPT))
    html = _search(client, when="arrive", at="08:00")
    assert re.search(r'value="arrive"[^>]*\n?\s*class="segmented-input" checked', html)
    assert 'name="at"' in html and 'value="08:00"' in html


def test_bad_time_falls_back_to_leave_now(client, at_time):
    at_time(datetime(2026, 10, 28, 7, 30, tzinfo=CPT))
    html = _search(client, when="arrive", at="nonsense")
    assert "Departures from 07:30" in html
