"""Per-route UCT timetable validity (Consolidation & Exam set, 26 Oct - 20 Nov 2026).

UCT's page says Route 5 runs to 26 Nov but its PDF says 20 Nov; we use the
earlier date (compliance), so every route ends 20 Nov. After a route's end
date its times are hidden and the "Timetable expired" banner is shown.
"""
from datetime import date
from pathlib import Path

import duckdb
import pytest

from etl.scrape_uct import (
    _parse_set_heading,
    _parse_validity_with_year,
    discover_routes,
    select_timetable_set,
)
from fastapi_app.services.uct_validity import (
    expired_routes,
    expiry_warning,
    is_expired,
    uct_expiry_state,
    uct_route_validity,
)

SET_NEW = "Routes valid from 26 October to 20 November 2026"
INDEX_HTML = f"""
<h2>Routes valid from 14 September to 25 October 2026</h2>
<div><h3><a href="/media/1">Route 1: Claremont (14 Sept – 25 Oct 2026)</a></h3></div>
<div><h3><a href="/media/2">Route 20: Great Westerford (14 Sept – 25 Oct 2026)</a></h3></div>
<h2>{SET_NEW}</h2>
<div><h3><a href="/media/11">Route 1: Claremont (26 Oct – 20 Nov 2026)</a></h3></div>
<div><h3><a href="/media/15">Route 5: Forest Hill via Mowbray (26 Oct – 26 Nov 2026)</a></h3></div>
<div><h3><a href="/media/99">Route: Overnight library service shuttle timetable (26 Oct - 20 Nov)</a></h3></div>
"""


def test_set_heading_and_yearless_dates():
    assert _parse_set_heading(SET_NEW) == ("2026-10-26", "2026-11-20")
    assert _parse_validity_with_year("Overnight (26 Oct - 20 Nov)", "2026") == (
        "2026-10-26", "2026-11-20")


def test_selects_current_set_and_skips_old_routes():
    assert select_timetable_set(INDEX_HTML, today="2026-10-28") == SET_NEW
    routes = discover_routes(INDEX_HTML, [], SET_NEW)
    by_id = {r["route_id"]: r for r in routes}
    assert set(by_id) == {"UCT1", "UCT5", "UCTOVN"}  # Route 20 not in this set
    assert by_id["UCT1"]["valid_until"] == "2026-11-20"
    assert by_id["UCT5"]["valid_until"] == "2026-11-26"
    assert by_id["UCTOVN"]["valid_from"] == "2026-10-26"
    assert by_id["UCTOVN"]["valid_until"] == "2026-11-20"


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE timetables (route_id VARCHAR, valid_from DATE, "
              "valid_until DATE, operator VARCHAR)")
    rows = [(f"UCT{n}", "2026-10-26", "2026-11-20") for n in (1, 2, 13, 14)]
    rows += [("UCT5", "2026-10-26", "2026-11-26"), ("UCTOVN", "2026-10-26", "2026-11-20")]
    c.executemany("INSERT INTO timetables VALUES (?, ?, ?, 'uct')", rows)
    c.execute("INSERT INTO timetables VALUES ('101', NULL, NULL, 'myciti')")
    return c


def test_is_expired_boundary():
    assert not is_expired(date(2026, 11, 20), date(2026, 11, 20))  # last valid day
    assert is_expired(date(2026, 11, 20), date(2026, 11, 21))
    assert not is_expired(None, date(2030, 1, 1))


def test_nothing_expired_during_set(con):
    assert uct_expiry_state(con, date(2026, 11, 20)) == (set(), None)


def test_banner_after_20_nov_route5_still_current(con):
    expired, banner = uct_expiry_state(con, date(2026, 11, 21))
    assert "UCT5" not in expired
    assert expired == {"UCT1", "UCT2", "UCT13", "UCT14", "UCTOVN"}
    assert banner.startswith("UCT shuttle timetable expired on 20 Nov 2026")
    assert "Overnight library loop" in banner and "Route 5" not in banner


def test_route5_ends_26_nov(con):
    assert "UCT5" not in uct_expiry_state(con, date(2026, 11, 26))[0]
    expired, banner = uct_expiry_state(con, date(2026, 11, 27))
    assert "UCT5" in expired
    assert "expired on 26 Nov 2026" in banner
    assert "live" not in banner.lower() and "gps" not in banner.lower()


def test_expiry_warning_none_when_empty():
    assert expiry_warning({}) is None
    assert expired_routes({"UCT1": (None, None)}, date(2030, 1, 1)) == {}


DB = Path(__file__).resolve().parents[1] / "data" / "myciti.duckdb"


@pytest.mark.skipif(not DB.exists(), reason="myciti.duckdb missing")
def test_loaded_consolidation_exam_set():
    c = duckdb.connect(str(DB), read_only=True)
    v = uct_route_validity(c)
    expected = {f"UCT{n}" for n in (1, 2, 3, 4, 5, 6, 7, 9, 10, 11, 13, 14, 15, 16)} | {"UCTOVN"}
    assert set(v) == expected  # Route 20 dropped: not in this set
    for rid in expected:  # incl. Route 5: earlier (PDF) date wins
        assert v[rid] == (date(2026, 10, 26), date(2026, 11, 20)), rid
    n = c.execute("SELECT COUNT(*) FROM departures WHERE route_id = 'UCTOVN' "
                  "AND day_type = 'weekday'").fetchone()[0]
    assert n == 48


@pytest.mark.skipif(not DB.exists(), reason="myciti.duckdb missing")
def test_search_hides_expired_routes_and_shows_banner(monkeypatch):
    """After 20 Nov the search page shows the banner and no expired UCT times."""
    from fastapi.testclient import TestClient
    import fastapi_app.services.uct_validity as uv
    from fastapi_app.main import app

    params = {"from_stop": "UC North", "to_stop": "Kramer",
              "day_type": "weekday", "operator": "uct"}
    with TestClient(app) as client:
        monkeypatch.setattr(uv, "today_cape_town", lambda: date(2026, 11, 1))
        during = client.get("/", params=params).text
        monkeypatch.setattr(uv, "today_cape_town", lambda: date(2026, 11, 21))
        after = client.get("/", params=params).text
    assert "UCT 13" in during and "Timetable expired" not in during
    assert "Timetable expired" in after
    assert "expired on 20 Nov 2026" in after
    assert "UCT 13" not in after and "UCT 14" not in after


def test_route5_override_is_earlier_date():
    from etl.scrape_uct import VALID_UNTIL_OVERRIDES
    assert VALID_UNTIL_OVERRIDES["UCT5"] == "2026-11-20"


@pytest.mark.skipif(not DB.exists(), reason="myciti.duckdb missing")
def test_loaded_route5_expires_with_others_after_20_nov():
    c = duckdb.connect(str(DB), read_only=True)
    expired, banner = uct_expiry_state(c, date(2026, 11, 21))
    assert "UCT5" in expired and "Route 5" in banner
    assert uct_expiry_state(c, date(2026, 11, 20)) == (set(), None)


# --- Cross-midnight trips (Routes 13/14) are left out: known gap -----------

def test_crosses_midnight_detection():
    from etl.scrape_uct import crosses_midnight
    assert crosses_midnight(["23:50:00", None, "23:55:00", "00:05:00"])
    assert not crosses_midnight(["00:05:00", None, "00:15:00"])
    assert not crosses_midnight(["22:00:00", "22:10:00"])


@pytest.mark.skipif(not DB.exists(), reason="myciti.duckdb missing")
def test_cross_midnight_trips_absent_for_13_14():
    c = duckdb.connect(str(DB), read_only=True)
    for rid in ("UCT13", "UCT14"):
        late = c.execute("SELECT COUNT(*) FROM departures WHERE route_id = ? "
                         "AND departure_time >= '23:15:00'", [rid]).fetchone()[0]
        assert late == 0, rid  # the 23:15 trip that ran into 00:xx is out
        n = c.execute("SELECT COUNT(*) FROM departures WHERE route_id = ? "
                      "AND day_type = 'weekday'", [rid]).fetchone()[0]
        assert n == {"UCT13": 47, "UCT14": 49}[rid]


# --- Static route footnotes ------------------------------------------------

def test_uct_notes_by_route_and_day():
    from fastapi_app.services.uct_notes import UCT_ROUTE_NOTES, uct_notes
    assert "Roscommon House" in uct_notes("UCT1", "weekday")[0]
    assert uct_notes("UCT1", "saturday") == []
    assert uct_notes(["UCT13", "UCT14"], "sunday") == [
        "Routes 13/14 at weekends, 15:35–21:50: board and alight at Roscommon House."]
    assert uct_notes("UCT13", "weekday") == []
    assert "visitor's pass" in uct_notes("UCTOVN", "weekday")[0]
    assert uct_notes("101", "weekday") == []
    for notes in UCT_ROUTE_NOTES.values():
        for _, text in notes:
            assert "live" not in text.lower() and "gps" not in text.lower()


def test_notes_are_escaped_not_safe(monkeypatch):
    from jinja2 import Environment
    import fastapi_app.services.uct_notes as un
    from fastapi_app.main import jinja_env
    src = Path(__file__).resolve().parents[1] / "fastapi_app" / "templates"
    for f in ("components/connection_card.html", "components/transfer_card.html",
              "pages/stop.html"):
        t = (src / f).read_text()
        assert "uct_notes(" in t
        assert "note|safe" not in t.replace(" ", "")
    monkeypatch.setitem(un.UCT_ROUTE_NOTES, "UCTX", [(None, "<b>x</b>")])
    out = jinja_env.from_string(
        "{% for note in uct_notes('UCTX') %}{{ note }}{% endfor %}").render()
    assert out == "&lt;b&gt;x&lt;/b&gt;"


@pytest.mark.skipif(not DB.exists(), reason="myciti.duckdb missing")
def test_notes_shown_in_search_and_stop_board(monkeypatch):
    from fastapi.testclient import TestClient
    import fastapi_app.services.uct_validity as uv
    from fastapi_app.main import app
    monkeypatch.setattr(uv, "today_cape_town", lambda: date(2026, 11, 1))
    with TestClient(app) as client:
        search = client.get("/", params={"from_stop": "UC North", "to_stop": "Kramer",
                                         "day_type": "saturday", "operator": "uct"}).text
        board = client.get("/stop/UC North", params={"day_type": "weekday",
                                                     "operator": "uct"}).text
    assert "UCT 13" in search
    assert "board and alight at Roscommon House" in search
    assert "leave from Roscommon House" in board  # Route 1 serves UC North
