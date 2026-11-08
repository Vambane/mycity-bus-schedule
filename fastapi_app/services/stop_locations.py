"""Where each searchable stop is, for "Use my location".

The browser asks for this list once and works out the nearest stop
itself, so the rider's position never leaves their device (that is the
promise in components/location_privacy.html).

Coordinates come from the City of Cape Town stops layer, matched to
stop names exactly as the network map does (system_map.py), so a stop
sits in the same place on the map and in "nearest stop". MyCiTi stops
and Metrorail stations are included: UCT shuttle stops have no
coordinates yet, and a UCT-only name must never borrow a MyCiTi stop's
position through a loose name match.
"""
from __future__ import annotations

from functools import lru_cache

import duckdb

from system_map import _load_stop_coords, _match_coords, _station_coords


@lru_cache(maxsize=1)
def _city_coords() -> dict[str, tuple[float, float]]:
    """The City stops layer, read once per process (it ships with the app)."""
    return _load_stop_coords()


@lru_cache(maxsize=8)
def _locate(names: tuple[str, ...]) -> tuple[tuple[str, float, float], ...]:
    """(name, lat, lon) for every name that matches a City stop, sorted by name."""
    matched = _match_coords(list(names), _city_coords())
    return tuple(
        (name, round(lat, 6), round(lon, 6))
        for name, (lat, lon) in sorted(matched.items())
    )


def stop_locations(conn: duckdb.DuckDBPyConnection, operator: str = "both") -> dict:
    """Stops riders can pick in search, with coordinates where known.

    Returns {"stops": [{"name", "lat", "lon"}], "unmapped": n} where
    *unmapped* counts searchable stops (for this network filter) that have
    no position, so the page can say when "nearest" may be incomplete.
    """
    def names(op: str | None) -> set[str]:
        sql = "SELECT DISTINCT stop_name FROM stops"
        rows = (conn.execute(sql + " WHERE operator = ?", [op]) if op
                else conn.execute(sql)).fetchall()
        return {row[0] for row in rows}

    myciti = tuple(sorted(names("myciti")))
    searchable = names(None if operator == "both" else operator)

    # Positions are MyCiTi stop positions plus Metrorail stations (which
    # carry their own coordinates and match by exact name only). A UCT stop
    # can share a name with a MyCiTi stop somewhere else entirely
    # ("Sandown"), so the UCT-only filter gets none until UCT stops have
    # coordinates of their own.
    located: list[dict] = []
    if operator in ("both", "myciti"):
        located += [
            {"name": name, "lat": lat, "lon": lon}
            for name, lat, lon in _locate(myciti)
            if name in searchable
        ]
    if operator in ("both", "metrorail"):
        located += [
            {"name": name, "lat": round(lat, 6), "lon": round(lon, 6)}
            for name, (lat, lon) in sorted(_station_coords().items())
            if name in searchable
        ]
    return {"stops": located, "unmapped": len(searchable) - len(located)}
