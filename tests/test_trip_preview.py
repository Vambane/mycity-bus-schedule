"""Trip preview eligibility, ghost position and Compliance caption."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pytest

from fastapi_app.services import trip_preview
from fastapi_app.services.trip_preview import (
    COMPLIANCE_CAPTION,
    OPTIONAL_CAPTION,
    PREVIEW_AHEAD_MIN,
    build_trip_preview,
    eligibility,
    ghost_position,
    status_text,
)

DB = Path(__file__).resolve().parents[1] / "data" / "myciti.duckdb"


@pytest.fixture(scope="module")
def con():
    if not DB.exists():
        pytest.skip("myciti.duckdb missing")
    c = duckdb.connect(str(DB), read_only=True)
    yield c
    c.close()


def test_compliance_caption_locked():
    assert COMPLIANCE_CAPTION == "Estimated from timetable — not live GPS."
    assert "Planning aid only" in OPTIONAL_CAPTION


def test_build_preview_saturday_sample(con):
    row = con.execute(
        "SELECT route_id, direction FROM departures "
        "WHERE day_type = 'saturday' LIMIT 1"
    ).fetchone()
    assert row
    stops = con.execute(
        """
        SELECT stop_name FROM departures
        WHERE route_id = ? AND direction = ? AND day_type = 'saturday'
        GROUP BY stop_name ORDER BY MIN(departure_time)
        """,
        list(row),
    ).fetchall()
    board, alight = stops[0][0], stops[min(4, len(stops) - 1)][0]
    deps = con.execute(
        """
        SELECT departure_time FROM departures
        WHERE route_id = ? AND direction = ? AND day_type = 'saturday'
              AND stop_name = ?
        ORDER BY departure_time
        """,
        [row[0], row[1], board],
    ).fetchall()
    dep = str(deps[len(deps) // 2][0])[:5]
    payload = build_trip_preview(
        con,
        route_id=row[0],
        direction=row[1],
        day_type="saturday",
        board=board,
        alight=alight,
        dep=dep,
    )
    assert payload["caption"] == COMPLIANCE_CAPTION
    if payload["eligible"]:
        assert payload["ghost"]["lat"]
        assert len(payload["path"]) >= 2
        assert payload["geometry_source"] in ("street", "schematic")
        assert payload["ghost_opacity"] == 0.45


# ---------------------------------------------------------------------------
# Where the ghost is drawn
# ---------------------------------------------------------------------------

_T01 = {"route_id": "T01", "direction": "To T01 Dunoon", "day_type": "weekday"}


def _stops(*rows):
    """Located stops as (name, minutes, distance, role)."""
    return [{"name": n, "time": f"{t // 60:02d}:{t % 60:02d}:00", "t": t, "d": d, "role": r}
            for n, t, d, r in rows]


LINE = _stops(
    ("A", 600, 0.0, "before"),
    ("B", 610, 1.0, "board"),
    ("C", 614, 3.0, "ride"),
    ("D", 620, 4.0, "alight"),
)


def test_ghost_waits_at_first_stop_before_the_trip_starts():
    pos = ghost_position(LINE, 590)
    assert pos["state"] == "not_started"
    assert pos["d"] == 0.0


def test_ghost_moves_by_time_and_distance_between_stops():
    # Halfway in time between C (3.0) and D (4.0) is halfway along that road.
    pos = ghost_position(LINE, 617)
    assert pos["state"] == "moving"
    assert (pos["prev"], pos["next"]) == (2, 3)
    assert pos["d"] == pytest.approx(3.5)
    # B to C covers twice the distance in four minutes: 1 min in is 1.5.
    assert ghost_position(LINE, 611)["d"] == pytest.approx(1.5)


def test_ghost_stops_at_the_last_stop():
    pos = ghost_position(LINE, 700)
    assert pos["state"] == "arrived"
    assert pos["d"] == 4.0


def test_status_names_the_rider_stop_until_the_bus_gets_there():
    assert status_text(ghost_position(LINE, 605), LINE) == (
        "Should be between A and B. Due at B at 10:10.")
    assert status_text(ghost_position(LINE, 615), LINE) == (
        "Should be between C and D. Due at D at 10:20.")
    assert status_text(ghost_position(LINE, 590), LINE) == (
        "Not on the road yet. Leaves A at 10:00.")
    assert status_text(ghost_position(LINE, 700), LINE) == (
        "Should have reached D at 10:20.")


def _at(monkeypatch, hour, minute):
    pinned = datetime(2026, 11, 13, hour, minute, tzinfo=ZoneInfo("Africa/Johannesburg"))
    monkeypatch.setattr(trip_preview, "_now_ct", lambda: pinned)


def _t01_pair(con):
    """A weekday T01 trip from Milnerton to Table View, or skip."""
    row = con.execute(
        """
        SELECT a.departure_time, b.departure_time
        FROM   departures a, departures b
        WHERE  a.route_id = 'T01' AND b.route_id = 'T01'
          AND  a.direction = 'To T01 Dunoon' AND b.direction = a.direction
          AND  a.day_type = 'weekday' AND b.day_type = 'weekday'
          AND  a.stop_name = 'Milnerton' AND b.stop_name = 'Table View'
          AND  a.departure_time BETWEEN '16:00:00' AND '17:00:00'
        ORDER BY a.departure_time LIMIT 1
        """
    ).fetchone()
    if not row:
        pytest.skip("no T01 Milnerton trip in data")
    return str(row[0])[:5]


def test_preview_not_offered_for_a_bus_hours_away(con, monkeypatch):
    dep = _t01_pair(con)
    hour, minute = int(dep[:2]), int(dep[3:])
    _at(monkeypatch, hour - 2, minute)
    ok, reason = eligibility(con=con, board="Milnerton", alight="Table View", dep=dep, **_T01)
    assert (ok, reason) == (False, "too_early")
    _at(monkeypatch, hour, minute - min(minute, PREVIEW_AHEAD_MIN - 5))
    ok, _ = eligibility(con=con, board="Milnerton", alight="Table View", dep=dep, **_T01)
    assert ok


def test_preview_follows_the_bus_before_the_rider_boards(con, monkeypatch):
    dep = _t01_pair(con)
    hour, minute = int(dep[:2]), int(dep[3:])
    _at(monkeypatch, hour, minute - min(minute, 10))
    payload = build_trip_preview(con, board="Milnerton", alight="Table View", dep=dep, **_T01)
    if not payload["eligible"]:
        pytest.skip(f"preview hidden: {payload['hide_reason']}")
    roles = [s["role"] for s in payload["stops"]]
    # The trip is followed back past the boarding stop, so the rider sees
    # the bus coming, and the boarding stop sits where board_index says.
    assert roles[0] == "before"
    assert roles[payload["board_index"]] == "board"
    assert roles[-1] == "alight"
    located = [s for s in payload["stops"] if "d" in s]
    assert [s["d"] for s in located] == sorted(s["d"] for s in located)
    assert payload["status"].startswith(("Should be between", "Not on the road yet"))
    assert payload["service_midnight_ms"] > 0
