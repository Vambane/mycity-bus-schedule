"""Which timetable runs on which date, and what the pages show after the last bus.

Covers services/service_day.py (public holidays, Sunday carry-over, the
live-board rule) and the two pages that use it: the stop board and
journey search. Page tests freeze the Cape Town clock so they behave the
same whatever time the suite runs.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

import fastapi_app.routes.search as search_route
import fastapi_app.routes.stop as stop_route
from fastapi_app.main import app
from fastapi_app.services import service_day as sd

CPT = ZoneInfo("Africa/Johannesburg")


# ---------------------------------------------------------------------------
# Calendar rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "year,expected",
    [
        (2000, date(2000, 4, 23)),
        (2024, date(2024, 3, 31)),
        (2025, date(2025, 4, 20)),
        (2026, date(2026, 4, 5)),
        (2027, date(2027, 3, 28)),
        (2038, date(2038, 4, 25)),
    ],
)
def test_easter_sunday(year, expected):
    assert sd.easter_sunday(year) == expected


def test_2026_public_holidays():
    holidays = sd.public_holidays(2026)
    assert holidays[date(2026, 4, 3)] == "Good Friday"
    assert holidays[date(2026, 4, 6)] == "Family Day"
    assert holidays[date(2026, 9, 24)] == "Heritage Day"
    # Women's Day 2026 is a Sunday, so Monday 10 August is also a holiday.
    assert holidays[date(2026, 8, 10)] == "National Women's Day (observed)"
    # Human Rights Day 2026 is a Saturday: no carry-over to Monday.
    assert date(2026, 3, 23) not in holidays
    assert len(holidays) == 13


def test_extra_holidays_can_be_declared(monkeypatch):
    election = date(2029, 5, 9)
    assert sd.holiday_name(election) is None
    monkeypatch.setitem(sd.EXTRA_HOLIDAYS, election, "General election")
    assert sd.holiday_name(election) == "General election"
    assert sd.day_type_for(election) == sd.SUNDAY


@pytest.mark.parametrize(
    "day,expected",
    [
        (date(2026, 10, 29), "weekday"),   # ordinary Thursday
        (date(2026, 10, 31), "saturday"),
        (date(2026, 11, 1), "sunday"),
        (date(2026, 9, 24), "sunday"),     # Heritage Day, a Thursday
        (date(2026, 8, 10), "sunday"),     # observed Monday holiday
        (date(2026, 12, 26), "sunday"),    # Day of Goodwill on a Saturday
    ],
)
def test_day_type_for(day, expected):
    assert sd.day_type_for(day) == expected


def test_next_service_day_crosses_into_weekend():
    friday_night = datetime(2026, 10, 30, 23, 0, tzinfo=CPT)
    assert sd.next_service_day(friday_night) == (date(2026, 10, 31), "saturday")


def test_live_board_only_for_todays_own_timetable():
    wednesday = datetime(2026, 10, 28, 21, 0, tzinfo=CPT)
    assert sd.is_live_board("weekday", wednesday)
    assert not sd.is_live_board("saturday", wednesday)
    assert not sd.is_live_board("weekday", wednesday, date(2026, 11, 2))
    assert sd.is_live_board("weekday", wednesday, date(2026, 10, 28))


# ---------------------------------------------------------------------------
# Pages, with the clock frozen
# ---------------------------------------------------------------------------

@pytest.fixture(name="at")
def fixture_at(monkeypatch):
    """Freeze both routes' clock at a given Cape Town time."""
    def freeze(when: datetime) -> None:
        monkeypatch.setattr(stop_route, "get_cape_town_time", lambda: when)
        monkeypatch.setattr(search_route, "get_cape_town_time", lambda: when)
    return freeze


@pytest.fixture(name="client", scope="module")
def fixture_client():
    with TestClient(app) as c:
        yield c


def test_stop_board_friday_night_shows_saturday_timetable(client, at):
    # Civic Centre: last bus 22:35; first weekday bus 04:54, first Saturday 05:30.
    at(datetime(2026, 10, 30, 23, 0, tzinfo=CPT))
    html = client.get("/stop/Civic Centre").text
    assert "No more buses today" in html
    assert "Saturday timetable" in html
    assert "05:30" in html
    assert "04:54" not in html  # the old fallback reused Friday's weekday times


def test_stop_board_midweek_night_shows_tomorrows_weekday_buses(client, at):
    at(datetime(2026, 10, 28, 23, 0, tzinfo=CPT))
    html = client.get("/stop/Civic Centre").text
    assert "No more buses today" in html
    assert "weekday timetable" in html
    assert "04:54" in html


def test_stop_board_other_day_shows_whole_day(client, at):
    # Browsing Saturday's board late on a Wednesday: the whole day, no banner.
    at(datetime(2026, 10, 28, 23, 0, tzinfo=CPT))
    html = client.get("/stop/Civic Centre", params={"day_type": "saturday"}).text
    assert "No more buses today" not in html
    assert "All saturday departures" in html
    assert "05:30" in html


def test_stop_board_public_holiday(client, at):
    # Heritage Day 2026 is a Thursday: Sunday timetable, and the page says why.
    at(datetime(2026, 9, 24, 9, 0, tzinfo=CPT))
    html = client.get("/stop/Civic Centre").text
    assert "Heritage Day" in html
    assert "Sunday timetables apply today" in html
    # The Sunday tab is the selected one.
    assert re.search(r'day_type=sunday[^"]*"\s*class="tab active"', html)


def test_search_friday_night_uses_saturday_timetable(client, at):
    # Civic Centre -> Camps Bay: first weekday 05:40, first Saturday 05:38.
    at(datetime(2026, 10, 30, 23, 0, tzinfo=CPT))
    html = client.get(
        "/search", params={"from_stop": "Civic Centre", "to_stop": "Camps Bay"}
    ).text
    assert "No more buses today" in html
    assert "Saturday timetable" in html
    assert "05:38" in html
    assert "05:40" not in html
    # Cards link to the Saturday journey, so the schematic page matches.
    assert "day_type=saturday" in html


def test_search_other_day_is_not_cut_off_by_the_clock(client, at):
    at(datetime(2026, 10, 28, 23, 0, tzinfo=CPT))
    html = client.get(
        "/search",
        params={"from_stop": "Civic Centre", "to_stop": "Camps Bay", "day_type": "saturday"},
    ).text
    assert "No more buses today" not in html
    assert "All departures" in html
    assert "05:38" in html
