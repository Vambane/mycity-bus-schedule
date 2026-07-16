"""
journey_map.py — Inline map preview for transfer journeys
==========================================================
Draws one itinerary on a small Leaflet map so a route chain like
"216 → T01 → 105 via Wood, Civic Centre" is legible geographically,
with the same Schematic / Street toggle as the system map: schematic
mode connects the leg's stops with octilinear 45/90 degree elbows
(official printed-map style), street mode follows the city's real route
geometries sliced between the boarding and alighting stops. Markers
label the origin, every changeover stop, and the destination.

Stop order along a leg is recovered from the timetable (the first trip
of the day visits stops in sequence), and coordinates come from the
city's stops layer via the same matching helpers the system map uses.
Stops that cannot be geolocated are skipped within a leg; an origin or
destination missing from the city layer (a handful of stops are) gets
an approximate marker at the nearest drawable point, labelled as such.
Legs whose route has no street geometry fall back to the stop-sequence
line in street mode. Only when no leg can be drawn is the map hidden.
"""

import json
import logging
import math

import duckdb
import streamlit.components.v1 as components

# Deliberately shared internals: the journey map must place stops exactly
# where the system map places them, and draw the same street geometries.
from system_map import _load_route_paths, _load_stop_coords, _match_coords

log = logging.getLogger(__name__)

# A stop must lie within ~500 m of a route geometry before its street
# slice is trusted (0.005 degrees at Cape Town's latitude).
_STREET_MATCH_TOL_SQ = 0.005 ** 2

# Marker fill colors by kind (origin green, transfer amber, destination red)
MARKER_COLORS = {
    "origin": "#188038",
    "transfer": "#b45309",
    "destination": "#c5221f",
}

_TILE_URL = "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
_ATTRIBUTION = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>'
    ' &copy; <a href="https://carto.com/attributions">CARTO</a>'
)

_HTML_TEMPLATE = """<!DOCTYPE html>
<html><head>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
  html, body, #jmap { margin: 0; height: 100%; }
  .jm-label { font-size: 11px; font-weight: 600; }
  #style-toggle { position: absolute; top: 10px; right: 10px; z-index: 1000;
    border-radius: 6px; overflow: hidden;
    box-shadow: 0 1px 5px rgba(0,0,0,0.4); }
  #style-toggle button { border: none; padding: 6px 11px; font-size: 12px;
    background: #fff; color: #333; cursor: pointer; }
  #style-toggle button.on { background: #0072BC; color: #fff;
    font-weight: 700; }
</style>
</head><body>
<div id="jmap"></div>
<div id="style-toggle">
  <button id="btn-schematic" class="on">Schematic</button>
  <button id="btn-street">Street</button>
</div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  const DATA = __DATA__;
  const map = L.map("jmap", {scrollWheelZoom: false});
  L.tileLayer("__TILES__", {attribution: '__ATTR__'}).addTo(map);

  // Octilinear elbow between two stops, as on the system map: move at 45
  // degrees until aligned with the target, then straight along the axis.
  // Computed in locally scaled lon/lat so the angle is visually true.
  function elbow(a, b) {
    const k  = Math.cos(a[0] * Math.PI / 180);
    const ax = a[1] * k, ay = a[0], bx = b[1] * k, by = b[0];
    const dx = bx - ax, dy = by - ay;
    const t  = Math.min(Math.abs(dx), Math.abs(dy));
    if (t < 1e-9) return [a, b];
    return [a, [ay + Math.sign(dy) * t, (ax + Math.sign(dx) * t) / k], b];
  }

  const schematicLayer = L.layerGroup();
  const streetLayer = L.layerGroup();
  const pts = [];

  DATA.legs.forEach(function (leg) {
    const style = {color: leg.color, weight: 4, opacity: 0.85};
    let squared = [leg.points[0]];
    for (let i = 1; i < leg.points.length; i++) {
      squared = squared.concat(
        elbow(leg.points[i - 1], leg.points[i]).slice(1));
    }
    schematicLayer.addLayer(L.polyline(squared, style));
    streetLayer.addLayer(L.polyline(leg.street, style));
    leg.points.forEach(function (p) {
      pts.push(p);
      // Stop dots live on the base map so both modes show them
      L.circleMarker(p, {radius: 2, color: leg.color, fillOpacity: 1})
        .addTo(map);
    });
    leg.street.forEach(function (p) { pts.push(p); });
  });

  schematicLayer.addTo(map);  // default matches the system map

  const KIND = __KIND_COLORS__;
  DATA.markers.forEach(function (m) {
    L.circleMarker([m.lat, m.lon], {
      radius: 7, color: "#fff", weight: 2,
      fillColor: KIND[m.kind], fillOpacity: 1,
    }).addTo(map).bindTooltip(m.label, {
      permanent: true, direction: "top", className: "jm-label",
    });
  });

  const btnS = document.getElementById("btn-schematic");
  const btnT = document.getElementById("btn-street");
  function setMode(schematic) {
    if (schematic) { streetLayer.remove(); schematicLayer.addTo(map); }
    else { schematicLayer.remove(); streetLayer.addTo(map); }
    btnS.classList.toggle("on", schematic);
    btnT.classList.toggle("on", !schematic);
  }
  btnS.onclick = function () { setMode(true); };
  btnT.onclick = function () { setMode(false); };

  map.fitBounds(pts, {padding: [30, 30]});
</script></body></html>
"""


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def leg_stop_sequence(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    con: duckdb.DuckDBPyConnection,
    route_id: str,
    direction: str,
    board: str,
    alight: str,
    day_type: str,
) -> list[str]:
    """Ordered stop names riding one line from board to alight, inclusive.

    Stop order is recovered from MIN(departure_time) per stop, the same
    first-trip trick used by the system map and journey search. Returns
    an empty list when either stop is missing or out of order.
    """
    rows = con.execute(
        """
        SELECT   stop_name
        FROM     departures
        WHERE    route_id = ? AND direction = ? AND day_type = ?
        GROUP BY stop_name
        ORDER BY MIN(departure_time)
        """,
        [route_id, direction, day_type],
    ).fetchall()
    names = [r[0] for r in rows]
    try:
        i, j = names.index(board), names.index(alight)
    except ValueError:
        return []
    if i >= j:
        return []
    return names[i:j + 1]


def _nearest_index(path: list[list[float]], point: list[float]) -> tuple[int, float]:
    """Index of the path vertex nearest a [lat, lon] point, plus distance².

    Distances are computed in a locally scaled lon/lat space (longitude
    scaled by cos(latitude)) so they are comparable in both axes.
    """
    k = math.cos(math.radians(point[0]))
    best_i, best_d = 0, math.inf
    for i, (lat, lon) in enumerate(path):
        d = (lat - point[0]) ** 2 + ((lon - point[1]) * k) ** 2
        if d < best_d:
            best_i, best_d = i, d
    return best_i, best_d


def _street_slice(
    paths: list[list[list[float]]],
    board_pt: list[float],
    alight_pt: list[float],
) -> list[list[float]] | None:
    """Slice a route's street geometry between two stop coordinates.

    Picks the polyline both stops project onto best, cuts it between the
    nearest vertices, and orients it board to alight. Returns None when
    either stop is too far from every polyline (wrong branch, missing
    geometry) so the caller can fall back to the stop-sequence line.
    """
    best_score = math.inf
    best_slice: tuple[list[list[float]], int, int] | None = None
    for path in paths:
        i, d_i = _nearest_index(path, board_pt)
        j, d_j = _nearest_index(path, alight_pt)
        if max(d_i, d_j) > _STREET_MATCH_TOL_SQ:
            continue
        if d_i + d_j < best_score:
            best_score = d_i + d_j
            best_slice = (path, i, j)
    if best_slice is None:
        return None
    path, i, j = best_slice
    if i == j:
        return None
    segment = path[min(i, j):max(i, j) + 1]
    if j < i:
        segment = segment[::-1]
    return segment


def _marker(kind: str, stop: str, label: str,
            coords: dict[str, tuple[float, float]]) -> dict | None:
    """Marker dict for a stop, or None when it cannot be geolocated."""
    if stop not in coords:
        return None
    lat, lon = coords[stop]
    return {
        "kind": kind, "label": label,
        "lat": round(lat, 5), "lon": round(lon, 5),
    }


def build_journey_map_data(
    con: duckdb.DuckDBPyConnection,
    legs: list[dict],
    day_type: str,
    colors: dict[str, str],
) -> dict | None:
    """Geometry payload for one itinerary: leg polylines and stop markers.

    Args:
        con: read connection to the timetable database.
        legs: itinerary legs as produced by find_transfer_connections
            (route_id, direction, board, alight, dep, arr per leg).
        day_type: departures.day_type value the itinerary was built for.
        colors: route_id to hex color map (from the system map network).

    Returns:
        {"legs": [...], "markers": [...]} for render_journey_map, or None
        when no leg has two locatable points (nothing worth drawing).
    """
    sequences = [
        leg_stop_sequence(
            con, leg["route_id"], leg["direction"],
            leg["board"], leg["alight"], day_type,
        )
        for leg in legs
    ]
    all_names = sorted({stop for seq in sequences for stop in seq})
    coords = _match_coords(all_names, _load_stop_coords())
    street_paths = _load_route_paths()

    leg_paths = []
    for leg, seq in zip(legs, sequences):
        points = [
            [round(coords[s][0], 5), round(coords[s][1], 5)]
            for s in seq if s in coords
        ]
        if len(points) < 2:
            continue
        # Street mode: the city's real geometry between the leg's located
        # endpoints; stop-sequence line when the route has none that fits.
        street = _street_slice(
            street_paths.get(leg["route_id"].upper(), []),
            points[0], points[-1],
        )
        leg_paths.append({
            "route_id": leg["route_id"],
            "color": colors.get(leg["route_id"], "#666"),
            "points": points,
            "street": street if street is not None else points,
        })

    if not leg_paths:
        log.warning("Journey map skipped: itinerary could not be geolocated")
        return None

    origin = _marker("origin", legs[0]["board"],
                     f"{legs[0]['board']} · dep {legs[0]['dep'][:5]}", coords)
    if origin is None:
        # Endpoint missing from the city layer: anchor it at the first
        # drawable point of the journey and say so.
        first = leg_paths[0]["points"][0]
        origin = {
            "kind": "origin", "lat": first[0], "lon": first[1],
            "label": f"{legs[0]['board']} (approx) · dep {legs[0]['dep'][:5]}",
        }

    destination = _marker("destination", legs[-1]["alight"],
                          f"{legs[-1]['alight']} · arr {legs[-1]['arr'][:5]}",
                          coords)
    if destination is None:
        last = leg_paths[-1]["points"][-1]
        destination = {
            "kind": "destination", "lat": last[0], "lon": last[1],
            "label": f"{legs[-1]['alight']} (approx) · "
                     f"arr {legs[-1]['arr'][:5]}",
        }

    markers = [origin]
    for leg in legs[1:]:
        markers.append(_marker(
            "transfer", leg["board"],
            f"{leg['board']} · change to {leg['route_id']} {leg['dep'][:5]}",
            coords,
        ))
    markers.append(destination)
    return {
        "legs": leg_paths,
        "markers": [m for m in markers if m is not None],
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _journey_map_html(data: dict) -> str:
    """Self-contained Leaflet HTML document for one itinerary payload."""
    return (
        _HTML_TEMPLATE
        .replace("__DATA__", json.dumps(data))
        .replace("__KIND_COLORS__", json.dumps(MARKER_COLORS))
        .replace("__TILES__", _TILE_URL)
        .replace("__ATTR__", _ATTRIBUTION)
    )


def render_journey_map(data: dict, height: int = 340) -> None:
    """Render the itinerary map as an embedded Leaflet component."""
    components.html(_journey_map_html(data), height=height)
