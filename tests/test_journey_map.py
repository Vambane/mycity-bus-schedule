"""
test_journey_map.py — Tests for the transfer journey map preview
=================================================================
Uses an in-memory DuckDB timetable and a synthetic stops layer (via a
monkeypatched system_map.CCT_STOPS_GEOJSON) to assert leg sequencing,
geometry building, the origin/destination geolocation guard, and the
self-contained Leaflet HTML.
"""

import json
from pathlib import Path

import duckdb
import pytest

import system_map
from journey_map import (
    _journey_map_html,
    build_journey_map_data,
    leg_stop_sequence,
)

COLORS = {"R1": "#111111", "R2": "#222222"}

LEGS = [
    {"route_id": "R1", "direction": "To R1 X", "board": "A",
     "alight": "X", "dep": "08:00:00", "arr": "08:20:00"},
    {"route_id": "R2", "direction": "To R2 B", "board": "X",
     "alight": "B", "dep": "08:30:00", "arr": "08:50:00"},
]


@pytest.fixture(name="db")
def _db() -> duckdb.DuckDBPyConnection:
    """R1 runs A → M → X, R2 runs X → B; M is an intermediate stop."""
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE departures (route_id VARCHAR, direction VARCHAR, "
        "stop_name VARCHAR, day_type VARCHAR, departure_time VARCHAR)"
    )
    con.executemany(
        "INSERT INTO departures VALUES (?, ?, ?, 'weekday', ?)",
        [
            ("R1", "To R1 X", "A", "08:00:00"),
            ("R1", "To R1 X", "M", "08:10:00"),
            ("R1", "To R1 X", "X", "08:20:00"),
            ("R2", "To R2 B", "X", "08:30:00"),
            ("R2", "To R2 B", "B", "08:50:00"),
        ],
    )
    return con


def _write_stops(path: Path, names: list[str]) -> None:
    """Synthetic city stops layer: one point per name, spread on a line."""
    features = [
        {
            "type": "Feature",
            "properties": {"STOP_NAME": name},
            "geometry": {"type": "Point", "coordinates": [18.4 + i / 100, -33.9]},
        }
        for i, name in enumerate(names)
    ]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))


@pytest.fixture(name="stops_layer")
def _stops_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Geolocate every stop the default fixtures use."""
    path = tmp_path / "stops.geojson"
    _write_stops(path, ["A", "M", "X", "B"])
    monkeypatch.setattr(system_map, "CCT_STOPS_GEOJSON", path)
    return path


def _write_routes(path: Path, rid: str, line: list[list[float]]) -> None:
    """Synthetic city routes layer: one LineString ([lon, lat] order)."""
    features = [{
        "type": "Feature",
        "properties": {"RT_NMBR": rid},
        "geometry": {"type": "LineString", "coordinates": line},
    }]
    path.write_text(json.dumps({"type": "FeatureCollection", "features": features}))


@pytest.fixture(name="routes_path", autouse=True)
def _routes_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Empty routes layer by default; street tests overwrite the file."""
    path = tmp_path / "routes.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": []}))
    monkeypatch.setattr(system_map, "CCT_ROUTES_GEOJSON", path)
    return path


# R1's street geometry: passes through A, M and X with detour vertices,
# then continues beyond X (the slice must stop at the alighting stop).
R1_STREET = [
    [18.400, -33.900], [18.405, -33.895], [18.410, -33.900],
    [18.415, -33.895], [18.420, -33.900], [18.425, -33.900],
]


# ---------------------------------------------------------------------------
# Leg sequencing
# ---------------------------------------------------------------------------

def test_leg_sequence_includes_intermediate_stops(db: duckdb.DuckDBPyConnection) -> None:
    """The leg passes through every stop between board and alight."""
    assert leg_stop_sequence(db, "R1", "To R1 X", "A", "X", "weekday") == ["A", "M", "X"]


def test_leg_sequence_rejects_reversed_or_unknown(db: duckdb.DuckDBPyConnection) -> None:
    """Reversed stop order or unknown stops give an empty sequence."""
    assert not leg_stop_sequence(db, "R1", "To R1 X", "X", "A", "weekday")
    assert not leg_stop_sequence(db, "R1", "To R1 X", "A", "Nowhere", "weekday")


# ---------------------------------------------------------------------------
# Geometry building
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("stops_layer")
def test_build_data_has_leg_paths_and_markers(db: duckdb.DuckDBPyConnection) -> None:
    """Two legs become two colored polylines plus three labelled markers."""
    data = build_journey_map_data(db, LEGS, "weekday", COLORS)
    assert data is not None
    assert [leg["route_id"] for leg in data["legs"]] == ["R1", "R2"]
    assert data["legs"][0]["color"] == "#111111"
    assert len(data["legs"][0]["points"]) == 3  # A, M, X
    kinds = [m["kind"] for m in data["markers"]]
    assert kinds == ["origin", "transfer", "destination"]
    assert "dep 08:00" in data["markers"][0]["label"]
    assert "change to R2" in data["markers"][1]["label"]
    assert "arr 08:50" in data["markers"][2]["label"]


def test_build_data_skips_unlocated_intermediate_stop(
    db: duckdb.DuckDBPyConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing intermediate coordinate thins the line but keeps the leg."""
    path = tmp_path / "stops.geojson"
    _write_stops(path, ["A", "X", "B"])  # no M
    monkeypatch.setattr(system_map, "CCT_STOPS_GEOJSON", path)
    data = build_journey_map_data(db, LEGS, "weekday", COLORS)
    assert data is not None
    assert len(data["legs"][0]["points"]) == 2  # A, X


def test_build_data_approximates_unlocated_origin(
    db: duckdb.DuckDBPyConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An origin missing from the city layer gets an approximate marker."""
    path = tmp_path / "stops.geojson"
    _write_stops(path, ["M", "X", "B"])  # no A
    monkeypatch.setattr(system_map, "CCT_STOPS_GEOJSON", path)
    data = build_journey_map_data(db, LEGS, "weekday", COLORS)
    assert data is not None
    origin = data["markers"][0]
    assert origin["kind"] == "origin"
    assert "(approx)" in origin["label"]
    # Anchored at the first drawable point of the journey (M on leg 1)
    assert [origin["lat"], origin["lon"]] == data["legs"][0]["points"][0]


def test_build_data_none_when_nothing_locates(
    db: duckdb.DuckDBPyConnection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No map at all when no leg has two locatable points."""
    path = tmp_path / "stops.geojson"
    _write_stops(path, ["Somewhere Else"])
    monkeypatch.setattr(system_map, "CCT_STOPS_GEOJSON", path)
    assert build_journey_map_data(db, LEGS, "weekday", COLORS) is None


# ---------------------------------------------------------------------------
# Street mode
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("stops_layer")
def test_street_mode_slices_route_geometry(
    db: duckdb.DuckDBPyConnection, routes_path: Path,
) -> None:
    """A leg's street line follows the city geometry between its stops."""
    _write_routes(routes_path, "R1", R1_STREET)
    data = build_journey_map_data(db, LEGS, "weekday", COLORS)
    leg1 = data["legs"][0]
    # Sliced between the vertices nearest A and X: 5 of the 6 vertices,
    # including the detours, excluding the tail beyond X.
    assert len(leg1["street"]) == 5
    assert leg1["street"][0] == [-33.9, 18.4]
    assert leg1["street"][-1] == [-33.9, 18.42]
    assert leg1["street"] != leg1["points"]


@pytest.mark.usefixtures("stops_layer")
def test_street_mode_orients_board_to_alight(
    db: duckdb.DuckDBPyConnection, routes_path: Path,
) -> None:
    """Geometry drawn in the opposite direction is reversed for the leg."""
    _write_routes(routes_path, "R1", list(reversed(R1_STREET)))
    data = build_journey_map_data(db, LEGS, "weekday", COLORS)
    leg1 = data["legs"][0]
    assert leg1["street"][0] == [-33.9, 18.4]     # board end first
    assert leg1["street"][-1] == [-33.9, 18.42]   # alight end last


@pytest.mark.usefixtures("stops_layer")
def test_street_mode_falls_back_without_geometry(
    db: duckdb.DuckDBPyConnection, routes_path: Path,
) -> None:
    """Routes missing from the city layer reuse the stop-sequence line."""
    _write_routes(routes_path, "R1", R1_STREET)  # nothing for R2
    data = build_journey_map_data(db, LEGS, "weekday", COLORS)
    leg2 = data["legs"][1]
    assert leg2["street"] == leg2["points"]


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("stops_layer")
def test_html_embeds_data_and_leaflet(db: duckdb.DuckDBPyConnection) -> None:
    """The document is self-contained: Leaflet, tiles, payload, markers."""
    data = build_journey_map_data(db, LEGS, "weekday", COLORS)
    html = _journey_map_html(data)
    assert "leaflet@1.9.4" in html
    assert "basemaps.cartocdn.com" in html
    assert json.dumps(data) in html
    assert "__DATA__" not in html and "__KIND_COLORS__" not in html
    # Schematic / Street toggle, with schematic elbows built client-side
    assert 'id="btn-schematic"' in html and 'id="btn-street"' in html
    assert "function elbow" in html
