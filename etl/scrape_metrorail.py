"""
etl/scrape_metrorail.py — Metrorail Western Cape timetables
============================================================
Loads PRASA's Western Cape train timetables into Halte's tables.

Source
------
PRASA publishes its timetables as image and PDF sheets. The open-source
Next Train project (github.com/enock-elk/next-train-astro) transcribes
those sheets into a station x train-number grid and publishes the result
as ``public/data/full-database.json``. This module reads that export.

    python3 run_etl.py --metrorail            # fetch and load
    python3 run_etl.py --metrorail --offline  # use data/metrorail_raw/

The fetched JSON is cached to ``data/metrorail_raw/`` (git-ignored) so an
offline rebuild never needs the network.

How trains become Halte lines
-----------------------------
Halte has no trips table: every feature (direct search, transfers, the
departure board, the journey page, the map) groups departures by
``route_id + direction`` and recovers stop order from the earliest time
at each stop. That works for a bus route whose trips all call at the
same stops. It breaks for rail, where one line mixes branches, short
workings and express trains.

So each Metrorail line is split into **stop patterns**: the set of trains
that call at exactly the same stations in the same order. A pattern
becomes one ``direction`` with a rider-readable label such as
"Cape Town to Simon's Town" or "Cape Town to Fish Hoek (express)".
Within a pattern every trip visits every stop, so the k-th time at each
station belongs to the k-th train and the existing engine pairs trips
exactly. The train number is kept in ``departures.trip_id``.

Data handling
-------------
- A train can be printed on more than one sheet (Kraaifontein to
  Bellville on one, Bellville to Cape Town on another). Its stops are
  merged. Where two sheets disagree on a time, the sheet on which the
  train has the most stops wins and the conflict is logged.
- Weekday and Saturday sheets are loaded. Metrorail runs no Sunday
  service. Public holiday sheets are skipped: Halte runs every operator
  on its Sunday timetable on public holidays, which would show holiday
  trains on ordinary Sundays (see METRORAIL_INTEGRATION.md).
- Stations are named "<Name> Station" so they never merge with a MyCiTi
  stop of the same name (Claremont, Mowbray, Strand, Woodstock, ...).
- Station coordinates from the export are written to
  ``data/metrorail_stations.csv`` for the map and "nearest stop".
"""
from __future__ import annotations

import csv
import json
import logging
import re
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

SOURCE_URL = (
    "https://raw.githubusercontent.com/enock-elk/next-train-astro/main/"
    "public/data/full-database.json"
)
SOURCE_PAGE = "https://www.prasa.com/train-schedules/"
DATA_DIR = Path(__file__).parent.parent / "data"
CACHE_PATH = DATA_DIR / "metrorail_raw" / "full-database.json"
STATIONS_CSV = DATA_DIR / "metrorail_stations.csv"
OPERATOR = "metrorail"

# Sheet suffix -> Halte day type. Public holiday ("pub") sheets are skipped.
DAY_TYPES = {"weekday": "weekday", "sat": "saturday"}

# Halte route ids for the five lines. "MR-" keeps them clear of MyCiTi
# (T/D/numeric) and UCT ids; route_name is what riders see on badges.
LINES = {
    "MR-S": "Southern Line",
    "MR-CF": "Cape Flats Line",
    "MR-C": "Central Line",
    "MR-N": "Northern Line",
    "MR-M": "Malmesbury Line",
}

# Sheet terminus code -> route id. A sheet named "<a>_to_<b>_<day>" belongs
# to the line of whichever end is not Cape Town ("ct").
_TERMINUS_LINE = {
    "simon": "MR-S",
    "rtret": "MR-CF",
    "malm": "MR-M",
    "hani": "MR-C", "nolu": "MR-C", "kap": "MR-C", "mutul": "MR-C",
    "bellv": "MR-N", "kraai": "MR-N", "strnd": "MR-N",
    "well": "MR-N", "eerst": "MR-N", "dtoit": "MR-N",
}

# Stations riders use to tell branches apart ("via Monte Vista"), most
# distinctive first. A "via" label names the first of these the variant
# pattern calls at; otherwise the first station the main pattern lacks.
_VIA_LANDMARKS = [
    "Monte Vista", "Century City", "Langa", "Pinelands", "Mutual",
    "Athlone", "Kuils River", "Stellenbosch", "Sarepta",
]

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_SHEET_RE = re.compile(r"^(?P<a>[a-z]+)_to_(?P<b>[a-z]+)_(?P<day>[a-z]+)$")
_DATE_RE = re.compile(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})")
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}


# ---------------------------------------------------------------------------
# Small parsers
# ---------------------------------------------------------------------------

def line_for_sheet(sheet: str) -> str | None:
    """Route id for a sheet key such as 'ct_to_simon_weekday'."""
    m = _SHEET_RE.match(sheet)
    if not m:
        return None
    a, b = m["a"], m["b"]
    if {a, b} == {"bellv", "mutul"}:  # Bellville to Mutual runs via Langa
        return "MR-C"
    for code in (a, b):
        if code != "ct" and code in _TERMINUS_LINE:
            return _TERMINUS_LINE[code]
    return None


def norm_time(value: Any) -> str | None:
    """'5:15' / '7:02:00 ' -> 'HH:MM:SS'; anything else (EXPRESS, via MV) -> None."""
    m = _TIME_RE.match(str(value).strip())
    if not m:
        return None
    return f"{int(m[1]):02d}:{int(m[2]):02d}:{int(m[3] or 0):02d}"


def parse_effective(meta: Any) -> date | None:
    """'8 June 2026' -> date(2026, 6, 8); 'Unspecified' -> None."""
    m = _DATE_RE.search(str(meta or ""))
    if not m or m[2].lower() not in _MONTHS:
        return None
    return date(int(m[3]), _MONTHS[m[2].lower()], int(m[1]))


def station_name(raw: Any) -> str:
    """'SIMON'S TOWN' -> "Simon's Town Station"; blank -> ''."""
    name = str(raw or "").strip()
    if not name:
        return ""
    # Title-case each word; capitalize() already lowercases the "'S" in "SIMON'S".
    return " ".join(w.capitalize() for w in name.split()) + " Station"


def strip_station(name: str) -> str:
    """"Cape Town Station" -> "Cape Town" (for direction labels)."""
    return name[:-len(" Station")] if name.endswith(" Station") else name


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_source(offline: bool = False, timeout: int = 60) -> dict:
    """Return the Western Cape block of the Next Train export.

    Live mode downloads the export and refreshes the cache. Offline mode
    (or a failed download with a cache present) reads the cache.
    """
    if not offline:
        try:
            req = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "Halte-ETL"})
            # nosec B310: fixed https URL, not user input
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_bytes(raw)
            log.info("Fetched Metrorail export (%d bytes) -> %s", len(raw), CACHE_PATH)
        except OSError as exc:
            if not CACHE_PATH.exists():
                raise
            log.warning("Fetch failed (%s); using cached %s", exc, CACHE_PATH)
    if not CACHE_PATH.exists():
        raise FileNotFoundError(
            f"No cached Metrorail export at {CACHE_PATH}. Run without --offline first."
        )
    return json.loads(CACHE_PATH.read_text(encoding="utf-8"))["westerncape"]


# ---------------------------------------------------------------------------
# Sheets -> trains
# ---------------------------------------------------------------------------

def _iter_sheets(wc: dict) -> Iterable[tuple[str, str, str, list, date | None]]:
    """(sheet, route_id, day_type, rows, effective) for every loadable sheet."""
    for key, rows in wc.items():
        if not isinstance(rows, list):
            continue
        m = _SHEET_RE.match(key)
        if not m or m["day"] not in DAY_TYPES:
            continue
        route_id = line_for_sheet(key)
        if route_id is None:
            log.warning("Skipping sheet with unknown line: %s", key)
            continue
        yield key, route_id, DAY_TYPES[m["day"]], rows, parse_effective(wc.get(key + "_meta"))


def _read_sheets(wc: dict) -> tuple[dict, dict, dict]:
    """Every (day, train) sighting per sheet, plus station coordinates.

    Returns:
        sightings: {(day_type, train_no): [{"sheet", "route_id", "stops"}]}
                   where stops are (row_seq, station, time)
        stations:  {station: (lat, lon)}
        effective: {(route_id, day_type): [effective dates of its sheets]}
    """
    sightings: dict[tuple[str, str], list[dict]] = defaultdict(list)
    stations: dict[str, tuple[float, float]] = {}
    effective: dict[tuple[str, str], list] = defaultdict(list)

    for sheet, route_id, day, sheet_rows, eff in _iter_sheets(wc):
        effective[(route_id, day)].append(eff)
        per_train: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
        for seq, row in enumerate(r for r in sheet_rows if isinstance(r, dict)):
            name = station_name(row.get("STATION"))
            if not name or str(row.get("STATION")).startswith("Last Updated"):
                continue
            _note_coords(stations, name, row.get("COORDINATES"))
            for col, val in row.items():
                t = norm_time(val) if col not in ("STATION", "COORDINATES") else None
                if t:
                    per_train[col.strip()].append((seq, name, t))
        for train, stops in per_train.items():
            sightings[(day, train)].append({"sheet": sheet, "route_id": route_id, "stops": stops})
    return sightings, stations, effective


def _note_coords(stations: dict, name: str, coords: Any) -> None:
    """Record a station's "lat, lon" the first time the export gives one."""
    if not coords or name in stations:
        return
    try:
        lat, lon = (float(x) for x in str(coords).split(","))
        stations[name] = (lat, lon)
    except ValueError:
        log.warning("Bad coordinates for %s: %r", name, coords)


def build_trains(wc: dict) -> tuple[dict, dict, list, dict]:
    """Merge every sheet into trains.

    Returns:
        trains:   {(day_type, train_no): {"route_id", "stops": [(time, station)]}}
        stations: {station: (lat, lon)}
        conflicts: [(day_type, train, station, kept, dropped, sheet)]
        effective: {(route_id, day_type): [effective dates of its sheets]}
    """
    sightings, stations, effective = _read_sheets(wc)
    trains: dict[tuple[str, str], dict] = {}
    conflicts: list[tuple] = []
    for key, seen in sightings.items():
        trains.update(_merge_sightings(key, seen, conflicts))
    return trains, stations, conflicts, effective


def _merge_sightings(key: tuple[str, str], seen: list[dict], conflicts: list) -> dict:
    """One train number's sightings -> {trip key: train}.

    The sheet showing most of the train is authoritative: its row order is
    the station order. Stations only on other sheets are slotted in by
    time. A sighting that does not line up is a different train (PRASA
    reuses numbers across sheets: 3515 runs to Wellington via Mutual at
    16:50 and to Bellville via Monte Vista at 18:10) and is kept apart as
    "<number>/2", "<number>/3", ...
    """
    seen = sorted(seen, key=lambda s: -len(s["stops"]))
    order = [(t, name) for _, name, t in sorted(seen[0]["stops"])]
    have = {name: t for t, name in order}
    out: dict[tuple[str, str], dict] = {}
    for s in seen[1:]:
        if not _same_train(have, s["stops"]):
            alt = (key[0], f"{key[1]}/{len(out) + 2}")
            alt_stops = [(t, name) for _, name, t in sorted(s["stops"])]
            out[alt] = {"route_id": s["route_id"], "stops": clean_stop_times(alt, alt_stops)}
            log.warning("%s train %s on %s is a different train; kept as %s",
                        key[0], key[1], s["sheet"], alt[1])
            continue
        for _, name, t in s["stops"]:
            if name in have:
                if have[name] != t:
                    conflicts.append((*key, name, have[name], t, s["sheet"]))
                continue
            pos = next((i for i, (ot, _) in enumerate(order) if ot > t), len(order))
            order.insert(pos, (t, name))
            have[name] = t
    out[key] = {"route_id": seen[0]["route_id"], "stops": clean_stop_times(key, order)}
    return out


def _minutes(hms: str) -> int:
    h, m, _ = hms.split(":")
    return int(h) * 60 + int(m)


def _same_train(have: dict[str, str], stops: list[tuple[int, str, str]],
                tolerance_min: int = 2) -> bool:
    """Whether a second sheet's sighting is the same physical train.

    It must share at least one station with what is known so far, and
    every shared station's time must agree within *tolerance_min* (sheets
    often print arrival on one and departure on the other).
    """
    shared = [(have[name], t) for _, name, t in stops if name in have]
    return bool(shared) and all(
        abs(_minutes(a) - _minutes(b)) <= tolerance_min for a, b in shared
    )


def clean_stop_times(key: tuple[str, str], stops: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Make one train's times strictly increase along its station order.

    A time earlier than the previous station's is a transcription error
    (it would put the station in the wrong place on every page), so it is
    dropped and logged. Two stations printed with the same minute get one
    second added to the later one: displays show HH:MM, and the engine's
    "earliest time = first stop" ordering becomes exact.
    """
    # Keep the longest non-decreasing run of times (fewest stations dropped),
    # so one mistyped time costs one station, not every station after it.
    n = len(stops)
    best = [1] * n
    prev = [-1] * n
    for i in range(n):
        for j in range(i):
            if stops[j][0] <= stops[i][0] and best[j] + 1 > best[i]:
                best[i], prev[i] = best[j] + 1, j
    keep: set[int] = set()
    i = max(range(n), key=lambda k: best[k]) if n else -1
    while i != -1:
        keep.add(i)
        i = prev[i]
    for k, (t, name) in enumerate(stops):
        if k not in keep:
            log.warning("%s train %s: dropped %s at %s (out of order with its neighbours)",
                        key[0], key[1], t, name)

    out: list[tuple[str, str]] = []
    for t, name in (s for k, s in enumerate(stops) if k in keep):
        if out and t <= out[-1][0]:
            # Same printed minute (possibly already bumped): one second later.
            t = out[-1][0]
        if out and t == out[-1][0]:
            h, m, s = (int(x) for x in out[-1][0].split(":"))
            s += 1
            t = f"{h + (m + s // 60) // 60:02d}:{(m + s // 60) % 60:02d}:{s % 60:02d}"
        out.append((t, name))
    return out


# ---------------------------------------------------------------------------
# Trains -> stop patterns
# ---------------------------------------------------------------------------

def label_patterns(patterns: dict[tuple[str, tuple[str, ...]], int]) -> dict:
    """Give every (route_id, stop tuple) pattern a unique, readable direction.

    The base label is "<origin> to <terminus>". When several patterns on
    one line share a base label, the one with the most trains keeps it;
    a pattern calling at a subset of its stations is "(express)"; any
    other gets "via <first station that differs>". A numeric suffix
    settles anything still tied.
    """
    by_base: dict[tuple[str, str], list[tuple[str, ...]]] = defaultdict(list)
    for (route_id, stops) in patterns:
        base = f"{strip_station(stops[0])} to {strip_station(stops[-1])}"
        by_base[(route_id, base)].append(stops)

    labels: dict[tuple[str, tuple[str, ...]], str] = {}
    for (route_id, base), group in by_base.items():
        group.sort(key=lambda s, rid=route_id: (-patterns[(rid, s)], -len(s), s))
        main_stops = group[0]
        used = {base}
        labels[(route_id, main_stops)] = base
        for stops in group[1:]:
            if set(stops) < set(main_stops):
                skipped = [strip_station(s) for s in main_stops if s not in stops]
                label = (f"{base} (skips {' and '.join(skipped)})"
                         if len(skipped) <= 2 else f"{base} (express)")
            else:
                extra = [s for s in stops if s not in main_stops]
                via = next((s for s in _VIA_LANDMARKS if f"{s} Station" in extra),
                           strip_station(extra[0]))
                label = f"{base} via {via}"
            n = 2
            candidate = label
            while candidate in used:
                candidate = f"{label} ({n})"
                n += 1
            used.add(candidate)
            labels[(route_id, stops)] = candidate
    return labels


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def scrape_all(offline: bool = False, wc: dict | None = None) -> dict[str, list[dict]]:
    """Build Halte rows (routes, stops, timetables, departures) for Metrorail.

    Args:
        offline: read the cached export instead of fetching.
        wc:      an already-loaded Western Cape block (tests pass fixtures).
    """
    if wc is None:
        wc = fetch_source(offline=offline)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    trains, stations, conflicts, effective = build_trains(wc)

    if conflicts:
        log.warning("%d train/station times disagree between sheets; kept the "
                    "fuller sheet's time:", len(conflicts))
        for c in conflicts[:20]:
            log.warning("  %s train %s at %s: kept %s, dropped %s (%s)", *c)

    # Drop single-stop "trains" (stray cells) and count trains per pattern.
    trains = {k: v for k, v in trains.items() if len(v["stops"]) >= 2}
    pattern_count: Counter = Counter(
        (v["route_id"], tuple(s for _, s in v["stops"])) for v in trains.values()
    )
    labels = label_patterns(dict(pattern_count))

    departures: list[dict] = []
    for (day, train), v in sorted(trains.items()):
        key = (v["route_id"], tuple(s for _, s in v["stops"]))
        for t, name in v["stops"]:
            departures.append({
                "route_id": v["route_id"], "stop_name": name,
                "direction": labels[key], "day_type": day,
                "departure_time": t, "trip_id": train,
                "operator": OPERATOR, "scraped_at": now,
            })

    stops: list[dict] = []
    for (route_id, pattern), label in sorted(labels.items(), key=lambda kv: (kv[0][0], kv[1])):
        for seq, name in enumerate(pattern, start=1):
            lat, lon = stations.get(name, (None, None))
            stops.append({
                "stop_id": f"{route_id}|{label}|{seq}", "stop_name": name,
                "route_id": route_id, "stop_sequence": seq, "direction": label,
                "stop_lat": lat, "stop_lon": lon,
                "operator": OPERATOR, "scraped_at": now,
            })

    used_routes = {d["route_id"] for d in departures}
    routes = [{
        "route_id": rid, "route_name": LINES[rid],
        "route_description": f"Metrorail Western Cape {LINES[rid]}",
        "detail_url": SOURCE_PAGE, "operator": OPERATOR, "scraped_at": now,
    } for rid in sorted(used_routes)]

    timetables = []
    for (rid, day), dates in sorted(effective.items()):
        if rid not in used_routes:
            continue
        known = [d for d in dates if d]
        timetables.append({
            "route_id": rid, "route_name": LINES[rid], "day_type": day,
            "timetable_url": SOURCE_PAGE,
            # Newest sheet date: the line is "as of" its latest sheet.
            "valid_from": max(known) if known else None,
            "valid_until": None, "operator": OPERATOR, "scraped_at": now,
        })

    write_stations_csv(stations)
    log.info(
        "Metrorail: %d lines, %d patterns, %d trains, %d departures, %d stations",
        len(routes), len(labels), len(trains), len(departures), len(stations),
    )
    return {"routes": routes, "stops": stops, "timetables": timetables,
            "departures": departures}


def write_stations_csv(stations: dict[str, tuple[float, float]],
                       path: Path | None = None) -> None:
    """Station coordinates for the map and "nearest stop".

    The default is read at call time so tests can point STATIONS_CSV at a
    temporary file.
    """
    path = path or STATIONS_CSV
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["stop_name", "lat", "lon"])
        for name, (lat, lon) in sorted(stations.items()):
            w.writerow([name, round(lat, 6), round(lon, 6)])


def main() -> None:
    """Parse and print row counts without loading (python3 etl/scrape_metrorail.py)."""
    import argparse  # pylint: disable=import-outside-toplevel

    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    ap = argparse.ArgumentParser(description="Metrorail Western Cape timetables")
    ap.add_argument("--offline", action="store_true")
    result = scrape_all(offline=ap.parse_args().offline)
    for table, table_rows in result.items():
        print(f"{table:12} {len(table_rows):6}")


if __name__ == "__main__":
    main()
