"""
test_uct_integration.py — Tests for UCT shuttle integration
=============================================================
Covers:
  - UCT scraper helpers (_normalise_time, _clean_stop_name, _classify_day_type,
    _parse_validity_dates)
  - Operator-scoped journey search (direct + transfer)
  - UCT route colors and category classification
  - Schema migration (operator column on existing DB)
  - Stale timetable detection
"""

import duckdb
import pytest
from datetime import date

from journey import find_connections, find_transfer_connections
from system_map import (
    _route_category,
    _route_color,
    get_route_colors,
    UCT_COLORS,
)

# Import scraper helpers — reach into etl package
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from etl.scrape_uct import (
    _normalise_time,
    _clean_stop_name,
    _classify_day_type,
    _parse_validity_dates,
)
from etl.load_db import DDL, MIGRATIONS


# ---------------------------------------------------------------------------
# Fixtures: in-memory DuckDB with operator column
# ---------------------------------------------------------------------------

def _make_multi_op_db(
    departures: list[tuple[str, str, str, str, str]],
) -> duckdb.DuckDBPyConnection:
    """In-memory DB from (route_id, direction, stop_name, departure_time, operator)."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE departures ("
        "route_id VARCHAR, direction VARCHAR, stop_name VARCHAR, "
        "day_type VARCHAR, departure_time VARCHAR, operator VARCHAR)"
    )
    con.executemany(
        "INSERT INTO departures VALUES (?, ?, ?, 'weekday', ?, ?)", departures
    )
    con.execute(
        "CREATE TABLE routes (route_id VARCHAR, route_name VARCHAR, operator VARCHAR)"
    )
    routes = {(r, op) for r, _, _, _, op in departures}
    con.executemany(
        "INSERT INTO routes VALUES (?, ?, ?)",
        [(r, f"{r} – Test", op) for r, op in routes],
    )
    return con


@pytest.fixture(name="mixed_db")
def _mixed_db() -> duckdb.DuckDBPyConnection:
    """DB with both MyCiTi and UCT routes.

    MyCiTi: 101 runs A→B
    UCT:    UCT1 runs C→D
    Shared stop B=C is served by both operators.
    """
    return _make_multi_op_db([
        # MyCiTi route
        ("101", "outbound", "A", "08:00:00", "myciti"),
        ("101", "outbound", "A", "09:00:00", "myciti"),
        ("101", "outbound", "B", "08:30:00", "myciti"),
        ("101", "outbound", "B", "09:30:00", "myciti"),
        # UCT route
        ("UCT1", "outbound", "C", "08:10:00", "uct"),
        ("UCT1", "outbound", "C", "09:10:00", "uct"),
        ("UCT1", "outbound", "D", "08:40:00", "uct"),
        ("UCT1", "outbound", "D", "09:40:00", "uct"),
    ])


# ===========================================================================
# UCT scraper helper tests
# ===========================================================================


class TestNormaliseTime:
    def test_valid_time(self):
        assert _normalise_time("8:30") == "08:30:00"

    def test_padded_time(self):
        assert _normalise_time("14:05") == "14:05:00"

    def test_dot_separator(self):
        assert _normalise_time("7.45") == "07:45:00"

    def test_with_whitespace(self):
        assert _normalise_time("  9:00  ") == "09:00:00"

    def test_invalid_returns_none(self):
        assert _normalise_time("not-a-time") is None

    def test_hour_too_large(self):
        assert _normalise_time("30:00") is None

    def test_minute_too_large(self):
        assert _normalise_time("08:60") is None


class TestCleanStopName:
    def test_normal_name(self):
        assert _clean_stop_name("Claremont") == "Claremont"

    def test_multiline_name(self):
        assert _clean_stop_name("Roscommon\nHouse") == "Roscommon House"

    def test_time_rejected(self):
        assert _clean_stop_name("8:30") is None

    def test_no_service_rejected(self):
        assert _clean_stop_name("no service") is None

    def test_long_text_rejected(self):
        assert _clean_stop_name("A" * 51) is None

    def test_empty_rejected(self):
        assert _clean_stop_name("") is None


class TestClassifyDayType:
    def test_weekday(self):
        assert _classify_day_type("Monday – Friday") == "weekday"

    def test_weekend(self):
        assert _classify_day_type("Weekend & Public Holidays") == "sunday"

    def test_saturday_and_sunday(self):
        assert _classify_day_type("Saturday & Sunday") == "sunday"

    def test_saturday_only(self):
        assert _classify_day_type("Saturday") == "saturday"

    def test_no_match(self):
        assert _classify_day_type("random text") is None


class TestParseValidityDates:
    def test_standard_format(self):
        assert _parse_validity_dates(
            "Core Service timetable : 14 Sept - 25 Oct 2026"
        ) == ("2026-09-14", "2026-10-25")

    def test_full_month_names(self):
        assert _parse_validity_dates(
            "Route 1 (1 January – 28 February 2026)"
        ) == ("2026-01-01", "2026-02-28")

    def test_no_match(self):
        assert _parse_validity_dates("No dates here") == (None, None)

    def test_em_dash(self):
        """En-dash and em-dash both accepted."""
        assert _parse_validity_dates(
            "14 Sept – 25 Oct 2026"
        ) == ("2026-09-14", "2026-10-25")


# ===========================================================================
# Operator-scoped journey search
# ===========================================================================


class TestOperatorDirectSearch:
    """find_connections honours the operator filter."""

    def test_myciti_only(self, mixed_db):
        df = find_connections(mixed_db, "A", "B", "weekday", operator="myciti")
        assert len(df) == 2
        assert set(df["route_id"]) == {"101"}

    def test_uct_only(self, mixed_db):
        df = find_connections(mixed_db, "C", "D", "weekday", operator="uct")
        assert len(df) == 2
        assert set(df["route_id"]) == {"UCT1"}

    def test_wrong_operator_empty(self, mixed_db):
        """UCT stops should return nothing when filtered to MyCiTi."""
        df = find_connections(mixed_db, "C", "D", "weekday", operator="myciti")
        assert df.empty

    def test_both_shows_all(self, mixed_db):
        """operator='both' is the default, no filtering."""
        df_ab = find_connections(mixed_db, "A", "B", "weekday", operator="both")
        assert len(df_ab) == 2
        df_cd = find_connections(mixed_db, "C", "D", "weekday", operator="both")
        assert len(df_cd) == 2

    def test_default_is_both(self, mixed_db):
        """No operator arg should behave like 'both'."""
        df = find_connections(mixed_db, "A", "B", "weekday")
        assert len(df) == 2


class TestOperatorTransferSearch:
    """find_transfer_connections honours the operator filter."""

    def test_cross_operator_transfer_when_both(self):
        """When operator='both', a MyCiTi→UCT transfer at a shared stop works."""
        con = _make_multi_op_db([
            ("101", "To X", "A", "08:00:00", "myciti"),
            ("101", "To X", "X", "08:20:00", "myciti"),
            ("UCT1", "To B", "X", "08:30:00", "uct"),
            ("UCT1", "To B", "B", "08:50:00", "uct"),
        ])
        df = find_transfer_connections(con, "A", "B", "weekday", operator="both")
        assert len(df) >= 1
        assert df.iloc[0]["route_ids"] == ["101", "UCT1"]

    def test_no_cross_operator_when_filtered(self):
        """Filtering to one operator prevents cross-operator transfers."""
        con = _make_multi_op_db([
            ("101", "To X", "A", "08:00:00", "myciti"),
            ("101", "To X", "X", "08:20:00", "myciti"),
            ("UCT1", "To B", "X", "08:30:00", "uct"),
            ("UCT1", "To B", "B", "08:50:00", "uct"),
        ])
        df = find_transfer_connections(con, "A", "B", "weekday", operator="myciti")
        assert df.empty


# ===========================================================================
# Route colors and categories
# ===========================================================================


class TestRouteCategoryAndColor:
    def test_uct_category(self):
        assert _route_category("UCT1") == "UCT Shuttle"
        assert _route_category("UCT15") == "UCT Shuttle"

    def test_trunk_category(self):
        assert _route_category("T01") == "Trunk"

    def test_direct_category(self):
        assert _route_category("D01") == "Direct"

    def test_area_category(self):
        assert _route_category("101") == "Area"

    def test_uct_color_from_palette(self):
        """UCT routes use the UCT_COLORS palette."""
        color = _route_color("UCT1", trunk_i=0, direct_i=0, area_i=0, uct_i=0)
        assert color == UCT_COLORS[0]

    def test_uct_color_wraps(self):
        """UCT index wraps around the palette length."""
        idx = len(UCT_COLORS) + 2
        color = _route_color("UCT99", trunk_i=0, direct_i=0, area_i=0, uct_i=idx)
        assert color == UCT_COLORS[2]

    def test_get_route_colors_filters_by_operator(self, mixed_db):
        """get_route_colors(operator='uct') returns only UCT routes."""
        colors = get_route_colors(mixed_db, operator="uct")
        assert all(rid.startswith("UCT") for rid in colors)
        assert len(colors) == 1  # UCT1 in the fixture

    def test_get_route_colors_both(self, mixed_db):
        """get_route_colors(operator='both') returns all routes."""
        colors = get_route_colors(mixed_db, operator="both")
        assert "101" in colors
        assert "UCT1" in colors


# ===========================================================================
# Schema migration (DDL + ALTER statements)
# ===========================================================================


class TestSchemaMigration:
    def test_ddl_creates_operator_column(self):
        """Fresh DDL includes the operator column on all tables."""
        con = duckdb.connect()
        con.execute(DDL)
        for table in ["routes", "stops", "timetables", "departures", "scrape_log"]:
            cols = [
                row[1] for row in
                con.execute(f"PRAGMA table_info('{table}')").fetchall()
            ]
            assert "operator" in cols, f"operator missing in {table}"

    def test_migrations_are_idempotent(self):
        """Running ALTER statements twice doesn't fail."""
        con = duckdb.connect()
        con.execute(DDL)
        for stmt in MIGRATIONS:
            con.execute(stmt)
        # Run again — IF NOT EXISTS must prevent errors
        for stmt in MIGRATIONS:
            con.execute(stmt)

    def test_timetables_has_validity_columns(self):
        """Fresh DDL includes valid_from and valid_until on timetables."""
        con = duckdb.connect()
        con.execute(DDL)
        cols = [
            row[1] for row in
            con.execute("PRAGMA table_info('timetables')").fetchall()
        ]
        assert "valid_from" in cols
        assert "valid_until" in cols


# ===========================================================================
# Stale timetable detection
# ===========================================================================


class TestStaleTimetable:
    def _make_timetable_db(self, valid_until_str: str) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect()
        con.execute(DDL)
        con.execute(
            "INSERT INTO timetables (id, route_id, day_type, operator, valid_until) "
            "VALUES (1, 'UCT1', 'weekday', 'uct', ?)",
            [valid_until_str],
        )
        return con

    def test_expired_detected(self):
        """A timetable valid_until in the past is stale."""
        con = self._make_timetable_db("2020-01-01")
        row = con.execute(
            "SELECT MAX(valid_until) FROM timetables WHERE operator = 'uct'"
        ).fetchone()
        assert row[0] is not None
        expiry = row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0]))
        assert expiry < date.today()

    def test_future_not_stale(self):
        """A timetable valid_until in the future is not stale."""
        con = self._make_timetable_db("2099-12-31")
        row = con.execute(
            "SELECT MAX(valid_until) FROM timetables WHERE operator = 'uct'"
        ).fetchone()
        expiry = row[0] if isinstance(row[0], date) else date.fromisoformat(str(row[0]))
        assert expiry > date.today()

    def test_no_uct_data_no_stale(self):
        """An empty timetables table returns NULL, no stale warning."""
        con = duckdb.connect()
        con.execute(DDL)
        row = con.execute(
            "SELECT MAX(valid_until) FROM timetables WHERE operator = 'uct'"
        ).fetchone()
        assert row[0] is None
