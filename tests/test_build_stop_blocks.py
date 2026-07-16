"""
test_build_stop_blocks.py — Tests for the stop-to-block polygon join
=====================================================================
Uses tiny synthetic GeoJSON fixtures (two unit-square polygons as blocks
1 and 2, three stops: one inside each square, one outside both) and a
temporary DuckDB database. No network access and no real CCT files.
"""

import json
from pathlib import Path

import duckdb
import pytest

import system_map
from etl.build_stop_blocks import _detect_block_property, build


def _write_areas(path: Path, block_prop: str = "BLOCK") -> None:
    """Write two unit-square block polygons (blocks 1 and 2) as GeoJSON."""
    def square(x0: float, y0: float) -> dict:
        return {
            "type": "Polygon",
            "coordinates": [[
                [x0, y0], [x0 + 1, y0], [x0 + 1, y0 + 1], [x0, y0 + 1], [x0, y0],
            ]],
        }

    features = [
        {"type": "Feature", "properties": {block_prop: 1}, "geometry": square(0, 0)},
        {"type": "Feature", "properties": {block_prop: 2}, "geometry": square(10, 0)},
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))


def _write_stops(path: Path) -> None:
    """Write three stop points: inside block 1, inside block 2, and outside."""
    def point(name: str, lon: float, lat: float) -> dict:
        return {
            "type": "Feature",
            "properties": {"STOP_NAME": name},
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
        }

    features = [
        point("Alpha", 0.5, 0.5),    # inside block 1
        point("Bravo", 10.5, 0.5),   # inside block 2
        point("Charlie", 50.0, 50.0),  # outside both
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))


def _make_db(path: Path) -> None:
    """Create a temp DuckDB with a departures table naming the three stops."""
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE departures (stop_name VARCHAR)")
    con.executemany(
        "INSERT INTO departures VALUES (?)",
        [("Alpha",), ("Bravo",), ("Charlie",)],
    )
    con.close()


@pytest.fixture(name="fixture_paths")
def _fixture_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Set up areas + stops GeoJSON and a temp DB; redirect the stops layer."""
    areas = tmp_path / "areas.geojson"
    stops = tmp_path / "stops.geojson"
    db = tmp_path / "test.duckdb"
    _write_areas(areas)
    _write_stops(stops)
    _make_db(db)
    monkeypatch.setattr(system_map, "CCT_STOPS_GEOJSON", stops)
    return {"areas": areas, "stops": stops, "db": db}


def _blocks(db: Path) -> dict[str, int | None]:
    """Read the stop_blocks table back as {stop_name: block}."""
    con = duckdb.connect(str(db), read_only=True)
    rows = con.execute("SELECT stop_name, block FROM stop_blocks").fetchall()
    con.close()
    return dict(rows)


# ---------------------------------------------------------------------------
# build()
# ---------------------------------------------------------------------------

def test_stops_inside_polygons_get_correct_blocks(fixture_paths: dict) -> None:
    """Each stop inside a square is assigned that square's block number."""
    mapped = build(fixture_paths["db"], fixture_paths["areas"])
    assert mapped == 2
    blocks = _blocks(fixture_paths["db"])
    assert blocks["Alpha"] == 1
    assert blocks["Bravo"] == 2


def test_stop_outside_all_polygons_gets_null(fixture_paths: dict) -> None:
    """A stop outside every polygon is kept with block = NULL."""
    build(fixture_paths["db"], fixture_paths["areas"])
    assert _blocks(fixture_paths["db"])["Charlie"] is None


def test_missing_areas_file_raises_and_leaves_db_untouched(
    fixture_paths: dict,
) -> None:
    """Without the polygon file, build raises and creates no table."""
    with pytest.raises(FileNotFoundError):
        build(fixture_paths["db"], fixture_paths["areas"].parent / "nope.geojson")
    con = duckdb.connect(str(fixture_paths["db"]), read_only=True)
    tables = {t[0] for t in con.execute("SHOW TABLES").fetchall()}
    con.close()
    assert "stop_blocks" not in tables


def test_parquet_snapshot_written(fixture_paths: dict) -> None:
    """The table is exported to snapshot/stop_blocks.parquet next to the DB."""
    build(fixture_paths["db"], fixture_paths["areas"])
    parquet = fixture_paths["db"].parent / "snapshot" / "stop_blocks.parquet"
    assert parquet.exists()
    con = duckdb.connect()
    count = con.execute(
        "SELECT COUNT(*) FROM read_parquet(?)", [str(parquet)]
    ).fetchone()[0]
    con.close()
    assert count == 3


def test_rerun_is_idempotent(fixture_paths: dict) -> None:
    """Running build twice leaves exactly one row per stop (full refresh)."""
    build(fixture_paths["db"], fixture_paths["areas"])
    build(fixture_paths["db"], fixture_paths["areas"])
    assert len(_blocks(fixture_paths["db"])) == 3


# ---------------------------------------------------------------------------
# _detect_block_property
# ---------------------------------------------------------------------------

def test_detect_known_property_name() -> None:
    """A well-known property name is picked directly."""
    features = [{"properties": {"BLOCK": 1}}, {"properties": {"BLOCK": 2}}]
    assert _detect_block_property(features) == "BLOCK"


def test_detect_known_property_case_insensitive() -> None:
    """Known names match regardless of case."""
    features = [{"properties": {"blok": 1}}, {"properties": {"blok": 2}}]
    assert _detect_block_property(features) == "blok"


def test_detect_falls_back_to_integer_heuristic() -> None:
    """An unknown property qualifies when its values are ints within 1..16."""
    features = [
        {"properties": {"NAME": "a", "LSB_NUM": "3"}},
        {"properties": {"NAME": "b", "LSB_NUM": "12"}},
    ]
    assert _detect_block_property(features) == "LSB_NUM"


def test_detect_raises_when_nothing_matches() -> None:
    """No plausible property means a ValueError listing what was seen."""
    features = [
        {"properties": {"NAME": "a", "CODE": 99}},
        {"properties": {"NAME": "b", "CODE": 100}},
    ]
    with pytest.raises(ValueError, match="NAME"):
        _detect_block_property(features)
