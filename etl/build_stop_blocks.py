"""
build_stop_blocks.py — Map stops to City of Cape Town load shedding blocks
===========================================================================
Point-in-polygon join of MyCiTi stop coordinates against the City of Cape
Town load shedding area polygons, producing a `stop_blocks` table
(stop_name, block 1..16) in DuckDB plus a Parquet snapshot export.

Stop coordinates come from data/cct_stops.geojson, matched to the scraped
stop names in the departures table via the same normalise-and-fuzzy-match
helpers the system map uses, so the table is keyed by the names the app
actually queries.

The polygon layer is NOT bundled with the repo. Download the
"Load shedding areas" dataset from the City of Cape Town Open Data Portal
(https://odp-cctegis.opendata.arcgis.com/) as GeoJSON and save it to:

    data/cct_loadshedding_areas.geojson

Without that file this step refuses to run (no block assignments are ever
fabricated). Stops that fall outside every polygon, or whose coordinates
cannot be resolved, get block = NULL and are counted in the log.

Usage:
    python3 etl/build_stop_blocks.py
"""

import json
import logging
import sys
from pathlib import Path

import duckdb
from shapely.geometry import Point, shape
from shapely.prepared import prep

# Make the project root importable (script layout, like run_etl.py)
sys.path.insert(0, str(Path(__file__).parent.parent))

# pylint: disable=wrong-import-position
# _load_stop_coords/_match_coords are deliberately shared internals: the
# block mapping must key on the same matched stop names as the system map.
from system_map import _load_stop_coords, _match_coords
from etl.load_db import DB_PATH

log = logging.getLogger(__name__)

AREAS_PATH = Path(__file__).parent.parent / "data" / "cct_loadshedding_areas.geojson"

MISSING_FILE_HELP = (
    "Load shedding area polygons not found.\n"
    f"  Expected file: {AREAS_PATH}\n"
    "  Download the 'Load shedding areas' dataset as GeoJSON from the\n"
    "  City of Cape Town Open Data Portal\n"
    "  (https://odp-cctegis.opendata.arcgis.com/) and save it to the path\n"
    "  above, then rerun: python3 etl/build_stop_blocks.py"
)

# Property names the block number is commonly published under
_BLOCK_PROPERTY_CANDIDATES = (
    "BLOCK", "LS_BLOCK", "BLOK", "LS_AREA", "AREA", "AREA_NUM", "ZONE",
)


# ---------------------------------------------------------------------------
# Polygon layer loading
# ---------------------------------------------------------------------------

def _detect_block_property(features: list[dict]) -> str:
    """Find the feature property that holds the 1..16 block number.

    Tries the well-known property names case-insensitively first, then
    falls back to the first property whose values are all integers within
    1..16 (with at least two distinct values, so a constant column never
    qualifies).

    Raises:
        ValueError: when no property looks like a block number; the
            message lists the properties that were seen.
    """
    if not features:
        raise ValueError("Polygon file contains no features")
    seen = list(features[0].get("properties", {}))
    lower_map = {p.lower(): p for p in seen}
    for candidate in _BLOCK_PROPERTY_CANDIDATES:
        if candidate.lower() in lower_map:
            prop = lower_map[candidate.lower()]
            log.info("Using block property %r (known name)", prop)
            return prop

    for prop in seen:
        values = set()
        for feat in features:
            raw = feat.get("properties", {}).get(prop)
            try:
                value = int(raw)
            except (TypeError, ValueError):
                values = set()
                break
            if not 1 <= value <= 16:
                values = set()
                break
            values.add(value)
        if len(values) >= 2:
            log.info("Using block property %r (integer 1..16 heuristic)", prop)
            return prop

    raise ValueError(
        f"No block-number property found; properties seen: {seen}"
    )


def _load_block_polygons(areas_path: Path) -> list[tuple[int, object]]:
    """Load the area polygons as [(block, prepared_geometry), ...].

    Handles Polygon and MultiPolygon geometries; features whose block value
    cannot be parsed as an int are skipped with a warning.
    """
    features = json.load(areas_path.open())["features"]
    prop = _detect_block_property(features)
    polygons: list[tuple[int, object]] = []
    for feat in features:
        raw = feat.get("properties", {}).get(prop)
        try:
            block = int(raw)
        except (TypeError, ValueError):
            log.warning("Skipping feature with non-integer block %r", raw)
            continue
        polygons.append((block, prep(shape(feat["geometry"]))))
    log.info("Loaded %d block polygons from %s", len(polygons), areas_path.name)
    return polygons


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build(db_path: Path = DB_PATH, areas_path: Path = AREAS_PATH) -> int:
    """Build the stop_blocks table and export its Parquet snapshot.

    Args:
        db_path: DuckDB database containing the departures table.
        areas_path: CCT load shedding area polygon GeoJSON.

    Returns:
        Number of stops assigned a block (NULL rows not counted).

    Raises:
        FileNotFoundError: when the polygon file is absent; raised before
            the database is opened so nothing is touched.
    """
    if not areas_path.exists():
        raise FileNotFoundError(MISSING_FILE_HELP)

    polygons = _load_block_polygons(areas_path)

    con = duckdb.connect(str(db_path))
    try:
        stop_names = [
            row[0] for row in
            con.execute("SELECT DISTINCT stop_name FROM departures").fetchall()
        ]
        # Same name-matching pipeline as the system map, so blocks line up
        # with the stops the app actually shows.
        coords = _match_coords(stop_names, _load_stop_coords())

        rows: list[tuple[str, int | None]] = []
        n_no_coords = n_outside = 0
        for name in stop_names:
            if name not in coords:
                rows.append((name, None))
                n_no_coords += 1
                continue
            lat, lon = coords[name]
            point = Point(lon, lat)  # GeoJSON order: (lon, lat)
            block = next(
                (b for b, poly in polygons if poly.contains(point)), None
            )
            if block is None:
                n_outside += 1
            rows.append((name, block))

        # Optional table: DDL lives here, not in load_db, so the core ETL
        # never depends on the polygon file being present.
        con.execute(
            "CREATE TABLE IF NOT EXISTS stop_blocks ("
            "stop_name VARCHAR PRIMARY KEY, block INTEGER)"
        )
        con.execute("DELETE FROM stop_blocks")  # full refresh, idempotent
        con.executemany("INSERT INTO stop_blocks VALUES (?, ?)", rows)

        snapshot_dir = db_path.parent / "snapshot"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        con.execute(
            f"COPY stop_blocks TO '{snapshot_dir / 'stop_blocks'}.parquet' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    finally:
        con.close()

    mapped = sum(1 for _, b in rows if b is not None)
    log.info(
        "stop_blocks built: %d mapped, %d without coordinates, "
        "%d outside all polygons (NULL)",
        mapped, n_no_coords, n_outside,
    )
    return mapped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    """Run the block mapping as a standalone step."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        build()
    except FileNotFoundError as err:
        print(err)
        sys.exit(1)


if __name__ == "__main__":
    _main()
