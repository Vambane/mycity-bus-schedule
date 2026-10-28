"""Per-route UCT timetable validity (Consolidation & Exam set, 26 Oct - 20 Nov 2026).

Route 5 runs to 26 Nov; every other route ends 20 Nov. After a route's end
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
    assert v["UCT5"] == (date(2026, 10, 26), date(2026, 11, 26))
    for rid in expected - {"UCT5"}:
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
