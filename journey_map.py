"""
journey_map.py — Inline map preview for transfer journeys
==========================================================
Draws one itinerary on a small Leaflet map so a route chain like
"216 → T01 → 105 via Wood, Civic Centre" is legible geographically:
each leg is a polyline through the stops the bus actually visits, with
markers for the origin, every changeover stop, and the destination.

Stop order along a leg is recovered from the timetable (the first trip
of the day visits stops in sequence), and coordinates come from the
city's stops layer via the same matching helpers the system map uses.
Stops that cannot be geolocated are skipped within a leg; an origin or
destination missing from the city layer (a handful of stops are) gets
an approximate marker at the nearest drawable point, labelled as such.
Only when no leg can be drawn at all is the map suppressed.
"""

import json
import logging

import duckdb
import streamlit.components.v1 as components

# Deliberately shared internals: the journey map must place stops exactly
# where the system map places them.
from system_map import _load_stop_coords, _match_coords

log = logging.getLogger(__name__)

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
</style>
</head><body><div id="jmap"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  const DATA = __DATA__;
  const map = L.map("jmap", {scrollWheelZoom: false});
  L.tileLayer("__TILES__", {attribution: '__ATTR__'}).addTo(map);

  const pts = [];
  DATA.legs.forEach(function (leg) {
    L.polyline(leg.points, {color: leg.color, weight: 4, opacity: 0.85})
      .addTo(map);
    leg.points.forEach(function (p) {
      pts.push(p);
      L.circleMarker(p, {radius: 2, color: leg.color, fillOpacity: 1})
        .addTo(map);
    });
  });

  const KIND = __KIND_COLORS__;
  DATA.markers.forEach(function (m) {
    L.circleMarker([m.lat, m.lon], {
      radius: 7, color: "#fff", weight: 2,
      fillColor: KIND[m.kind], fillOpacity: 1,
    }).addTo(map).bindTooltip(m.label, {
      permanent: true, direction: "top", className: "jm-label",
    });
  });

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

    leg_paths = []
    for leg, seq in zip(legs, sequences):
        points = [
            [round(coords[s][0], 5), round(coords[s][1], 5)]
            for s in seq if s in coords
        ]
        if len(points) >= 2:
            leg_paths.append({
                "route_id": leg["route_id"],
                "color": colors.get(leg["route_id"], "#666"),
                "points": points,
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
