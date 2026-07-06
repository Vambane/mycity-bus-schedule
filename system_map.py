"""
system_map.py — Interactive MyCiTi System Map
==============================================
Builds an interactive, geographically accurate map of the MyCiTi system,
overlaid on Cape Town street tiles.

Data sources:
  - Stop order per route+direction: derived from the departures table
    (MIN(departure_time) per stop — the first trip visits stops in order).
  - Stop coordinates: City of Cape Town open-data "MyCiTi Bus Stops" layer
    (data/cct_stops.geojson), matched to scraped stop names.
  - Street-accurate route lines: the city's "MyCiTi Bus Routes" layer
    (data/cct_routes.geojson), keyed by route number.

Rendered by a Leaflet Streamlit custom component (map_component/index.html)
with a toggle between square-ish schematic lines (45°/90° bends, like the
official printed map) and the exact street-following geometries. Clicking
a stop returns its name to Python via the component protocol — sandboxed
iframes cannot navigate the parent page, so URL tricks do not work.
"""

import json
import re
from collections import defaultdict
from difflib import get_close_matches
from pathlib import Path
from typing import Optional

import duckdb
import streamlit.components.v1 as components

DATA_DIR = Path(__file__).parent / "data"
CCT_STOPS_GEOJSON = DATA_DIR / "cct_stops.geojson"
CCT_ROUTES_GEOJSON = DATA_DIR / "cct_routes.geojson"

# Bidirectional custom component: renders the d3 map, returns clicked stop
_map_component = components.declare_component(
    "myciti_system_map",
    path=str(Path(__file__).parent / "map_component"),
)

# ---------------------------------------------------------------------------
# Route colors — grouped like the official map legend
# (trunk = reds, direct = blues, area = rotating palette)
# ---------------------------------------------------------------------------

TRUNK_COLORS = ["#E4003A", "#F0609E", "#C8102E", "#FF6900"]
DIRECT_COLORS = ["#0072BC", "#00A9E0", "#003DA5", "#41B6E6", "#5C88DA",
                 "#0085CA", "#71C5E8", "#009CDE"]
AREA_COLORS = [
    "#00843D", "#84BD00", "#FFB81C", "#7D55C7", "#00B2A9", "#DA291C",
    "#AA0061", "#FF8200", "#658D1B", "#6787B7", "#B58500", "#9063CD",
    "#00A376", "#E56DB1", "#4E5B31", "#C4622D", "#007398", "#93328E",
    "#A08629", "#41748D", "#D50032", "#254AA5", "#727D33", "#BB29BB",
    "#008C95", "#B86125", "#582C83", "#6BA539", "#003B5C", "#CE0058",
]


def _route_color(route_id: str, trunk_i: int, direct_i: int, area_i: int) -> str:
    """Pick a color from the palette matching the route's category."""
    if route_id.startswith("T"):
        return TRUNK_COLORS[trunk_i % len(TRUNK_COLORS)]
    if route_id.startswith("D"):
        return DIRECT_COLORS[direct_i % len(DIRECT_COLORS)]
    return AREA_COLORS[area_i % len(AREA_COLORS)]


def _route_category(route_id: str) -> str:
    """Official map legend category for a route."""
    if route_id.startswith("T"):
        return "Trunk"
    if route_id.startswith("D"):
        return "Direct"
    return "Area"


# ---------------------------------------------------------------------------
# Geolocation: match scraped stop names to the city's stops layer
# ---------------------------------------------------------------------------

def _norm_stop(name: str) -> str:
    """
    Normalise a stop name for matching: lowercase, strip the trailing
    platform number the city layer uses ("La Paloma 1" → "la paloma"),
    drop punctuation (apostrophe variants like "Graaff's Pool").
    """
    s = name.lower().strip()
    s = re.sub(r"\s+\d+$", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _load_stop_coords() -> dict[str, tuple[float, float]]:
    """
    Load the city stops layer and return {normalised name: (lat, lon)}.

    Multiple platforms share a name ("La Paloma 1/2") — their coordinates
    are averaged, but only within 500 m of the median point, so two distinct
    far-apart stops that happen to share a name don't get merged into a
    midpoint in the sea.
    """
    grouped: dict[str, list[tuple[float, float]]] = defaultdict(list)
    features = json.load(CCT_STOPS_GEOJSON.open())["features"]
    for f in features:
        name = f["properties"].get("STOP_NAME") or ""
        lon, lat = f["geometry"]["coordinates"]
        key = _norm_stop(name)
        if key:
            grouped[key].append((lat, lon))

    coords: dict[str, tuple[float, float]] = {}
    for key, pts in grouped.items():
        # Use the coordinate-wise median as the cluster reference so a single
        # outlier platform doesn't skew which points pass the 500 m filter.
        mid = len(pts) // 2
        ref_lat = sorted(p[0] for p in pts)[mid]
        ref_lon = sorted(p[1] for p in pts)[mid]
        # ~0.005° ≈ 500 m at Cape Town's latitude
        cluster = [p for p in pts
                   if abs(p[0] - ref_lat) < 0.005 and abs(p[1] - ref_lon) < 0.005]
        coords[key] = (
            sum(p[0] for p in cluster) / len(cluster),
            sum(p[1] for p in cluster) / len(cluster),
        )
    return coords


def _load_route_paths() -> dict[str, list[list[list[float]]]]:
    """
    Load the city routes layer → {ROUTE_ID: [path, ...]} where each path
    is a [[lat, lon], ...] street-following polyline. Route numbers are
    upper-cased to align with scraped route_ids ("214a" → "214A").
    """
    paths: dict[str, list] = defaultdict(list)
    for f in json.load(CCT_ROUTES_GEOJSON.open())["features"]:
        rid = (f["properties"].get("RT_NMBR") or "").upper()
        geom = f["geometry"]
        lines = ([geom["coordinates"]] if geom["type"] == "LineString"
                 else geom["coordinates"])
        for line in lines:
            # GeoJSON is [lon, lat]; Leaflet wants [lat, lon]. Thin points
            # closer than ~10 m to their predecessor — invisible at city
            # zoom but roughly halves the payload shipped to the browser.
            thinned: list[list[float]] = []
            for pt in line:
                lat, lon = round(pt[1], 5), round(pt[0], 5)
                if (not thinned
                        or abs(lat - thinned[-1][0]) > 1e-4
                        or abs(lon - thinned[-1][1]) > 1e-4):
                    thinned.append([lat, lon])
            if len(thinned) >= 2:
                paths[rid].append(thinned)
    return dict(paths)


def _match_coords(
    stop_names: list[str],
    coords: dict[str, tuple[float, float]],
) -> dict[str, tuple[float, float]]:
    """
    Map each scraped stop name to city coordinates: exact normalised match
    first, then a conservative fuzzy match (longer names only, high cutoff)
    to absorb spelling variants like "Graafs Pool" / "Graaff's Pool".
    """
    matched: dict[str, tuple[float, float]] = {}
    keys = list(coords)
    for name in stop_names:
        key = _norm_stop(name)
        if key in coords:
            matched[name] = coords[key]
        elif len(key) > 4:
            close = get_close_matches(key, keys, n=1, cutoff=0.85)
            if close:
                matched[name] = coords[close[0]]
    return matched


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_network(con: duckdb.DuckDBPyConnection) -> dict:
    """
    Build the system-map graph from the departures table.

    Stop order along each route+direction is recovered by sorting stops on
    their earliest departure of the day — the first trip visits stops in
    sequence, so MIN(departure_time) reproduces the printed stop order.

    Returns:
        {"nodes": [...], "links": [...], "routes": [...]} ready to embed
        as JSON in the d3 component.
    """
    # Earliest weekday departure per stop, per route direction; weekday has
    # the fullest service so every stop on the line appears.
    ordered = con.execute(
        """
        SELECT   route_id,
                 direction,
                 stop_name,
                 MIN(departure_time) AS first_dep
        FROM     departures
        WHERE    day_type = 'weekday'
        GROUP BY route_id, direction, stop_name
        ORDER    BY route_id, direction, first_dep
        """
    ).df()

    route_names = dict(
        con.execute("SELECT route_id, route_name FROM routes").fetchall()
    )

    # --- assign colors per route, grouped by category like the legend ---
    colors: dict[str, str] = {}
    trunk_i = direct_i = area_i = 0
    for route_id in sorted(ordered["route_id"].unique()):
        colors[route_id] = _route_color(route_id, trunk_i, direct_i, area_i)
        if route_id.startswith("T"):
            trunk_i += 1
        elif route_id.startswith("D"):
            direct_i += 1
        else:
            area_i += 1

    # --- geolocate stops via the city open-data layer ---
    all_stop_names = ordered["stop_name"].unique().tolist()
    geo = _match_coords(all_stop_names, _load_stop_coords())

    # --- links: consecutive stop pairs along each route direction ---
    # Only geolocated stops can be drawn; a stop without coordinates is
    # skipped and its neighbours joined directly, keeping the line whole.
    # The same physical segment appears in both directions; dedupe on the
    # unordered stop pair per route so each segment is drawn once.
    links: list[dict] = []
    seen_segments: set[tuple] = set()
    node_routes: dict[str, set] = {}

    for (route_id, _direction), grp in ordered.groupby(
        ["route_id", "direction"], sort=False
    ):
        stops = [s for s in grp["stop_name"] if s in geo]
        for stop in stops:
            node_routes.setdefault(stop, set()).add(route_id)
        for a, b in zip(stops, stops[1:]):
            key = (route_id, *sorted((a, b)))
            if a != b and key not in seen_segments:
                seen_segments.add(key)
                links.append({"source": a, "target": b, "route": route_id})

    # --- nodes: one per geolocated stop, sized by route count ---
    nodes = [
        {
            "id": stop,
            "routes": sorted(routes),
            "n": len(routes),
            "lat": round(geo[stop][0], 5),
            "lon": round(geo[stop][1], 5),
        }
        for stop, routes in node_routes.items()
    ]

    # --- street-accurate route geometries (city layer; some routes miss) ---
    paths = {rid: p for rid, p in _load_route_paths().items() if rid in colors}

    # --- legend entries grouped by category ---
    routes_meta = [
        {
            "id": rid,
            "name": route_names.get(rid, rid),
            "color": colors[rid],
            "category": _route_category(rid),
        }
        for rid in sorted(colors)
    ]

    return {
        "nodes": nodes,
        "links": links,
        "routes": routes_meta,
        "paths": paths,
        "coverage": {"located": len(geo), "total": len(all_stop_names)},
    }


# ---------------------------------------------------------------------------
# Component wrapper
# ---------------------------------------------------------------------------

def render_system_map(graph: dict) -> Optional[str]:
    """
    Render the interactive system map and return the clicked stop name.

    Returns None until the user clicks a stop; after a click, every rerun
    returns the same stop name until a different stop is clicked — callers
    must track the last handled value to avoid re-triggering.
    """
    return _map_component(graph=graph, key="system_map", default=None)
