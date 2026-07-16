"""
test_journey_transfers.py — Tests for the transfer journey search
==================================================================
Builds tiny in-memory DuckDB timetables and asserts one-transfer and
two-transfer itineraries, the changeover buffer and wait bounds, the
same-route exclusion, overnight GTFS times, and the Pareto reduction.
"""

import duckdb
import pytest

from journey import (
    MAX_WAIT_MIN,
    MIN_TRANSFER_MIN,
    find_transfer_connections,
)


def _make_db(departures: list[tuple[str, str, str, str]]) -> duckdb.DuckDBPyConnection:
    """In-memory DB from (route_id, direction, stop_name, departure_time) rows."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE departures ("
        "route_id VARCHAR, direction VARCHAR, stop_name VARCHAR, "
        "day_type VARCHAR, departure_time VARCHAR)"
    )
    con.executemany(
        "INSERT INTO departures VALUES (?, ?, ?, 'weekday', ?)", departures
    )
    con.execute("CREATE TABLE routes (route_id VARCHAR, route_name VARCHAR)")
    routes = {r for r, _, _, _ in departures}
    con.executemany(
        "INSERT INTO routes VALUES (?, ?)", [(r, f"{r} – Test Route") for r in routes]
    )
    return con


@pytest.fixture(name="one_transfer_db")
def _one_transfer_db() -> duckdb.DuckDBPyConnection:
    """R1 runs A→X twice, R2 runs X→B twice; A→B needs one change at X."""
    return _make_db([
        ("R1", "To R1 X", "A", "08:00:00"),
        ("R1", "To R1 X", "A", "09:00:00"),
        ("R1", "To R1 X", "X", "08:20:00"),
        ("R1", "To R1 X", "X", "09:20:00"),
        ("R2", "To R2 B", "X", "08:30:00"),
        ("R2", "To R2 B", "X", "09:40:00"),
        ("R2", "To R2 B", "B", "08:50:00"),
        ("R2", "To R2 B", "B", "10:00:00"),
    ])


# ---------------------------------------------------------------------------
# One transfer
# ---------------------------------------------------------------------------

def test_one_transfer_found(one_transfer_db: duckdb.DuckDBPyConnection) -> None:
    """A→B connects via X with correct legs, waits and totals."""
    df = find_transfer_connections(one_transfer_db, "A", "B", "weekday")
    assert list(df["transfers"].unique()) == [1]
    first = df.iloc[0]
    assert (first["dep"], first["arr"]) == ("08:00:00", "08:50:00")
    assert first["route_ids"] == ["R1", "R2"]
    assert first["via"] == ["X"]
    assert first["duration_min"] == 50
    leg1, leg2 = first["legs"]
    assert (leg1["board"], leg1["alight"], leg1["wait_min"]) == ("A", "X", 0)
    assert (leg2["board"], leg2["alight"], leg2["wait_min"]) == ("X", "B", 10)


def test_both_departures_paired(one_transfer_db: duckdb.DuckDBPyConnection) -> None:
    """Each origin departure gets its own itinerary when feasible."""
    df = find_transfer_connections(one_transfer_db, "A", "B", "weekday")
    assert list(df["dep"]) == ["08:00:00", "09:00:00"]
    assert list(df["arr"]) == ["08:50:00", "10:00:00"]


def test_reverse_direction_empty(one_transfer_db: duckdb.DuckDBPyConnection) -> None:
    """B→A is impossible: stop order on both lines points the other way."""
    assert find_transfer_connections(one_transfer_db, "B", "A", "weekday").empty


def test_min_transfer_buffer_respected() -> None:
    """An onward bus leaving under the changeover buffer is not caught."""
    con = _make_db([
        ("R1", "To R1 X", "A", "08:00:00"),
        ("R1", "To R1 X", "X", "08:20:00"),
        # Departs 1 min after arrival: under MIN_TRANSFER_MIN, must be skipped
        ("R2", "To R2 B", "X", "08:21:00"),
        ("R2", "To R2 B", "B", "08:41:00"),
        # Next service is within the window and should be used instead
        ("R2", "To R2 B", "X", "08:35:00"),
        ("R2", "To R2 B", "B", "08:55:00"),
    ])
    df = find_transfer_connections(con, "A", "B", "weekday")
    assert len(df) == 1
    assert df.iloc[0]["legs"][1]["dep"] == "08:35:00"
    assert df.iloc[0]["legs"][1]["wait_min"] >= MIN_TRANSFER_MIN


def test_max_wait_respected() -> None:
    """No itinerary when the only onward bus exceeds the wait bound."""
    con = _make_db([
        ("R1", "To R1 X", "A", "08:00:00"),
        ("R1", "To R1 X", "X", "08:20:00"),
        ("R2", "To R2 B", "X", "10:00:00"),  # 100 min wait > MAX_WAIT_MIN
        ("R2", "To R2 B", "B", "10:20:00"),
    ])
    assert MAX_WAIT_MIN < 100
    assert find_transfer_connections(con, "A", "B", "weekday").empty


def test_same_route_transfer_excluded() -> None:
    """Changing between directions of the same route is not a journey."""
    con = _make_db([
        ("R1", "To R1 X", "A", "08:00:00"),
        ("R1", "To R1 X", "X", "08:20:00"),
        ("R1", "To R1 A", "X", "08:30:00"),
        ("R1", "To R1 A", "B", "08:50:00"),
    ])
    assert find_transfer_connections(con, "A", "B", "weekday").empty


def test_overnight_times_supported() -> None:
    """GTFS hours past 24:00 pair and compare correctly across legs."""
    con = _make_db([
        ("R1", "To R1 X", "A", "23:50:00"),
        ("R1", "To R1 X", "X", "24:10:00"),
        ("R2", "To R2 B", "X", "24:20:00"),
        ("R2", "To R2 B", "B", "24:40:00"),
    ])
    df = find_transfer_connections(con, "A", "B", "weekday")
    assert len(df) == 1
    assert df.iloc[0]["duration_min"] == 50


# ---------------------------------------------------------------------------
# Two transfers
# ---------------------------------------------------------------------------

def test_two_transfers_via_bridge_line() -> None:
    """With no shared stop, a bridging line yields a three-leg itinerary."""
    con = _make_db([
        ("R1", "To R1 X1", "A", "08:00:00"),
        ("R1", "To R1 X1", "X1", "08:15:00"),
        ("RM", "To RM X2", "X1", "08:25:00"),
        ("RM", "To RM X2", "X2", "08:45:00"),
        ("R2", "To R2 B", "X2", "08:55:00"),
        ("R2", "To R2 B", "B", "09:10:00"),
    ])
    df = find_transfer_connections(con, "A", "B", "weekday")
    assert len(df) == 1
    row = df.iloc[0]
    assert row["transfers"] == 2
    assert row["route_ids"] == ["R1", "RM", "R2"]
    assert row["via"] == ["X1", "X2"]
    assert (row["dep"], row["arr"], row["duration_min"]) == (
        "08:00:00", "09:10:00", 70,
    )
    assert [leg["wait_min"] for leg in row["legs"]] == [0, 10, 10]


def test_two_transfers_only_when_one_is_impossible(
    one_transfer_db: duckdb.DuckDBPyConnection,
) -> None:
    """max_transfers=1 disables the bridge search."""
    con = _make_db([
        ("R1", "To R1 X1", "A", "08:00:00"),
        ("R1", "To R1 X1", "X1", "08:15:00"),
        ("RM", "To RM X2", "X1", "08:25:00"),
        ("RM", "To RM X2", "X2", "08:45:00"),
        ("R2", "To R2 B", "X2", "08:55:00"),
        ("R2", "To R2 B", "B", "09:10:00"),
    ])
    assert find_transfer_connections(con, "A", "B", "weekday", max_transfers=1).empty
    # And a viable one-transfer pair never produces two-transfer rows
    df = find_transfer_connections(one_transfer_db, "A", "B", "weekday")
    assert set(df["transfers"]) == {1}


# ---------------------------------------------------------------------------
# Result shaping
# ---------------------------------------------------------------------------

def test_pareto_drops_dominated_itineraries() -> None:
    """An earlier bus arriving later than a later bus is dominated."""
    con = _make_db([
        # Slow line: leaves 08:00, reaches X late (long leg)
        ("R1", "To R1 X", "A", "08:00:00"),
        ("R1", "To R1 X", "A", "08:30:00"),
        ("R1", "To R1 X", "X", "09:30:00"),
        ("R1", "To R1 X", "X", "09:35:00"),
        ("R2", "To R2 B", "X", "09:40:00"),
        ("R2", "To R2 B", "B", "09:50:00"),
    ])
    df = find_transfer_connections(con, "A", "B", "weekday")
    # Both origin departures reach the same onward bus; only the later
    # departure survives (same arrival, shorter journey).
    assert len(df) == 1
    assert df.iloc[0]["dep"] == "08:30:00"


def test_unknown_stop_returns_empty_frame() -> None:
    """Unknown stops produce an empty frame with the documented columns."""
    con = _make_db([
        ("R1", "To R1 X", "A", "08:00:00"),
        ("R1", "To R1 X", "X", "08:20:00"),
    ])
    df = find_transfer_connections(con, "Nowhere", "X", "weekday")
    assert df.empty
    assert list(df.columns) == [
        "dep", "arr", "duration_min", "duration",
        "transfers", "route_ids", "via", "legs",
    ]
