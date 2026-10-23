"""Trip preview eligibility and Compliance caption."""
from pathlib import Path

import duckdb
import pytest

from fastapi_app.services.trip_preview import (
    COMPLIANCE_CAPTION,
    OPTIONAL_CAPTION,
    build_trip_preview,
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
