"""
etl/scrape_myciti.py — MyCiTi Web Scraper (v2)
================================================
Scrapes all MyCiTi Western Cape routes and timetables.

Strategy (based on live HTML inspection):
  - Routes page is JS-rendered → useless for scraping.
  - Timetable downloads page lists all 38 routes as PDF links:
      /docs/route-timetables/{route_id}-timetable.pdf
  - Each PDF is downloaded and parsed with pdfplumber to extract:
      • Stop names (table column/row headers)
      • Departure times per stop per day type (Weekday / Saturday / Sunday)

Run standalone to test:
    python3 etl/scrape_myciti.py
"""

import io
import re
import time
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
import pdfplumber
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://www.myciti.org.za"
TIMETABLES_PAGE = f"{BASE_URL}/en/timetables/timetable-downloads/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-ZA,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# Polite delay between PDF downloads (seconds)
PDF_DELAY = 1.2

# Regex: matches times like 05:30, 5:30, 24:15 (GTFS overnight)
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")

# Direction header inside a PDF page, e.g. "Direction: To 101 Vredehoek"
DIRECTION_RE = re.compile(r"direction:\s*(?:to\s+)?(.+)", re.IGNORECASE)

# Cells that mark a departure/arrival column, not a stop or a time
MARKER_CELLS = {"dep", "arr", "dep.", "arr.", "-", ""}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_html(url: str, session: requests.Session) -> Optional[BeautifulSoup]:
    """Fetch a URL and return BeautifulSoup, or None on failure."""
    try:
        log.info(f"GET {url}")
        r = session.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except requests.RequestException as exc:
        log.warning(f"  HTML fetch failed: {exc}")
        return None


def _get_pdf_bytes(
    url: str,
    session: requests.Session,
    retries: int = 3,
) -> Optional[bytes]:
    """
    Download a PDF and return raw bytes, or None on failure.

    Retries with exponential backoff — the MyCiTi server intermittently
    drops or throttles requests mid-crawl, and a single missed PDF means
    a whole route silently disappears from the timetable.
    """
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            time.sleep(PDF_DELAY)
            return r.content
        except requests.RequestException as exc:
            log.warning(f"  PDF download attempt {attempt}/{retries} failed ({url}): {exc}")
            if attempt < retries:
                time.sleep(PDF_DELAY * (2 ** attempt))
    return None


# ---------------------------------------------------------------------------
# Step 1 — scrape route list from timetable downloads page
# ---------------------------------------------------------------------------

def scrape_route_list(session: requests.Session) -> list[dict]:
    """
    Extract all routes from the timetable downloads page.

    Each route appears as a PDF link:
        <a href="/docs/route-timetables/101-timetable.pdf">
            101Vredehoek - Gardens - Civic Centre (clockwise)
        </a>

    The text has no space between route ID and description, e.g. "101Vredehoek…"
    We split on the first non-alphanumeric boundary after the ID.

    Returns list of dicts: {route_id, route_name, route_description, pdf_url, scraped_at}
    """
    soup = _get_html(TIMETABLES_PAGE, session)
    if soup is None:
        return []

    now = datetime.now(timezone.utc).isoformat()
    routes = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]

        # Only PDF timetable links
        if "/docs/route-timetables/" not in href or not href.endswith(".pdf"):
            continue

        full_url = href if href.startswith("http") else BASE_URL + href

        # Route ID comes from the filename: "101-timetable.pdf" → "101"
        filename_stem = href.split("/")[-1].replace("-timetable.pdf", "")
        route_id = filename_stem.upper()

        if route_id in seen:
            continue
        seen.add(route_id)

        # Link text is "101Vredehoek - …" — split ID prefix from description
        raw_text = a.get_text(strip=True)

        # Try using the .route-item-label / .route-item-title spans inside the link
        label_span = a.select_one(".route-item-label, .route-label")
        title_span = a.select_one(".route-item-title")

        if label_span and title_span:
            description = title_span.get_text(strip=True)
        else:
            # Fallback: strip the route_id prefix from raw text
            # e.g. "101Vredehoek - Gardens" → "Vredehoek - Gardens"
            description = re.sub(r"^[A-Z]?\d+[A-Za-z]?\s*", "", raw_text).strip()
            if not description:
                description = raw_text

        route_name = f"{route_id} – {description}"

        routes.append({
            "route_id": route_id,
            "route_name": route_name,
            "route_description": description,
            "detail_url": full_url,   # PDF URL used as detail_url
            "scraped_at": now,
        })

    log.info(f"Found {len(routes)} routes on timetable downloads page.")
    return routes


# ---------------------------------------------------------------------------
# Step 2 — PDF timetable parsing
# ---------------------------------------------------------------------------

def _normalise_time(raw: str) -> Optional[str]:
    """
    Convert 'HH:MM' → 'HH:MM:SS'. Returns None if not a valid time.
    Accepts GTFS overnight hours (24:xx–29:xx) but rejects junk like 99:99.
    """
    raw = raw.strip().replace(".", ":")
    if TIME_RE.match(raw):
        h, m = raw.split(":")
        if int(h) < 30 and int(m) < 60:
            return f"{int(h):02d}:{m}:00"
    return None


def _classify_day_type(text: str) -> Optional[str]:
    """
    Return a day_type string if the text looks like a day-type heading.

    MyCiTi PDF page headers use plural forms:
        "MONDAYS TO FRIDAYS …"           → weekday
        "SATURDAYS …"                    → saturday
        "SUNDAYS AND PUBLIC HOLIDAYS …"  → sunday

    Sunday is checked before weekday because the Sunday heading also
    mentions public holidays (which follow the Sunday timetable).
    Returns 'weekday', 'saturday', 'sunday', or None.
    """
    t = text.lower()
    if re.search(r"\bsaturdays?\b", t):
        return "saturday"
    if re.search(r"\bsundays?\b|\bpublic.?holidays?\b", t):
        return "sunday"
    if re.search(r"\bweekdays?\b|\bmondays?\b", t):
        return "weekday"
    return None


def _extract_direction(text: str) -> Optional[str]:
    """
    Extract the direction/destination from a page header line, e.g.
    "Direction: To 101 Vredehoek" → "To 101 Vredehoek".
    Returns None if the line is not a direction header.
    """
    m = DIRECTION_RE.match(text.strip())
    if m:
        direction = " ".join(m.group(1).split())
        return f"To {direction}" if not direction.lower().startswith("to ") else direction
    return None


def _clean_stop_name(raw: str) -> Optional[str]:
    """
    Normalise a stop-name cell. Returns None if the cell is not a real
    stop name (empty, a dash, a Dep/Arr marker, or a time value).
    """
    # Collapse newlines / repeated whitespace from PDF cell wrapping
    name = " ".join(raw.split()).strip("_ ").strip()
    # Strip a trailing Dep/Arr marker that got merged into the name cell
    name = re.sub(r"\s+(dep|arr)\.?$", "", name, flags=re.IGNORECASE).strip()
    if not name or name.lower() in MARKER_CELLS or _normalise_time(name):
        return None
    return name


def _parse_timetable_table(
    table: list[list[str]],
    route_id: str,
    day_type: str,
    direction: str,
    now: str,
) -> tuple[list[dict], list[dict]]:
    """
    Parse a single pdfplumber table into stop and departure records.

    MyCiTi PDF tables are laid out one ROW per stop (verified against the
    live PDFs, e.g. 101-timetable.pdf):

        ['Civic Centre', 'Dep', '05:50', '06:50', '07:50', ...]
        ['Adderley',     '',    '05:53', '06:53', '07:53', ...]
        ['Wexford',      'Arr', '06:12', '07:12', '08:12', ...]

    Col 0 = stop name, col 1 = optional Dep/Arr marker, remaining cols =
    one departure time per trip ('-' means the trip skips that stop).
    If a table arrives transposed (stop names across row 0 instead), we
    detect that by checking where the time values sit and flip it first.

    Returns (stops_list, departures_list).
    """
    if not table or len(table) < 2:
        return [], []

    # Clean: replace None with ""
    clean = [[str(c).strip() if c else "" for c in row] for row in table]

    # Orientation check: in the normal layout the time cells live in the
    # body of each row and col 0 holds names. If instead col 0 is full of
    # times, the table is transposed — flip it so rows = stops.
    times_in_col0 = sum(1 for r in clean if r and _normalise_time(r[0]))
    names_in_col0 = sum(1 for r in clean if r and _clean_stop_name(r[0]))
    if times_in_col0 > names_in_col0:
        max_cols = max(len(r) for r in clean)
        padded = [r + [""] * (max_cols - len(r)) for r in clean]
        clean = list(map(list, zip(*padded)))  # transpose

    stops: list[dict] = []
    departures: list[dict] = []

    for seq, row in enumerate(clean, start=1):
        if not row:
            continue

        # Col 0 must be a real stop name — skip header/junk rows
        stop_name = _clean_stop_name(row[0])
        if stop_name is None:
            continue

        # Every time-like cell in the rest of the row is one trip's
        # departure from this stop; '-' (trip skips stop) is ignored.
        times = [t for c in row[1:] if (t := _normalise_time(c))]
        if not times:
            continue

        stops.append({
            "stop_id": f"{route_id}_{seq:03d}",
            "stop_name": stop_name,
            "route_id": route_id,
            "stop_sequence": seq,
            "direction": direction,
            "stop_lat": None,
            "stop_lon": None,
            "scraped_at": now,
        })

        for t in times:
            departures.append({
                "route_id": route_id,
                "stop_name": stop_name,
                "direction": direction,
                "day_type": day_type,
                "departure_time": t,
                "scraped_at": now,
            })

    return stops, departures


def parse_pdf_timetable(
    route_id: str,
    pdf_bytes: bytes,
) -> tuple[list[dict], list[dict]]:
    """
    Parse a MyCiTi PDF timetable and return (stops, departures).

    Handles multi-page PDFs and day-type sections (Weekday / Saturday / Sunday).
    Each page is scanned for text that signals a day-type change.

    Args:
        route_id:  The route identifier (e.g. '101', 'T01').
        pdf_bytes: Raw PDF content.

    Returns:
        (stops, departures) — deduplicated stop list and all departure times.
    """
    now = datetime.now(timezone.utc).isoformat()
    all_stops: list[dict] = []
    all_departures: list[dict] = []
    seen_stops: set[str] = set()
    seen_departures: set[tuple] = set()

    # Both carry over across pages: multi-page day/direction sections
    # repeat the header, but carrying over is a safe fallback.
    current_day_type = "weekday"
    current_direction = "outbound"

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                try:
                    # 1. Scan the page header for day-type and direction lines.
                    #    Each MyCiTi page states both above its table, e.g.
                    #    "SATURDAYS …" / "Direction: To 101 Vredehoek".
                    page_text = page.extract_text() or ""
                    for line in page_text.splitlines()[:6]:
                        dt = _classify_day_type(line)
                        if dt:
                            current_day_type = dt
                        direction = _extract_direction(line)
                        if direction:
                            current_direction = direction

                    # 2. Extract tables from this page
                    tables = page.extract_tables(
                        table_settings={
                            "vertical_strategy": "lines",
                            "horizontal_strategy": "lines",
                            "snap_tolerance": 5,
                        }
                    )

                    # Fallback: try text-based strategy if no tables found
                    if not tables:
                        tables = page.extract_tables(
                            table_settings={
                                "vertical_strategy": "text",
                                "horizontal_strategy": "text",
                            }
                        )

                    for table in (tables or []):
                        page_stops, page_deps = _parse_timetable_table(
                            table, route_id, current_day_type, current_direction, now
                        )

                        # De-duplicate stops (same stop repeats across day-type
                        # pages and across both directions). stop_id is assigned
                        # here from the order of first appearance — table-local
                        # sequences restart per direction and would collide.
                        for s in page_stops:
                            key = f"{s['route_id']}_{s['stop_name']}"
                            if key not in seen_stops:
                                seen_stops.add(key)
                                s["stop_id"] = f"{route_id}_{len(seen_stops):03d}"
                                all_stops.append(s)

                        # De-duplicate departures: continuation tables on the
                        # next page can repeat the boundary trip column
                        for d in page_deps:
                            key = (
                                d["route_id"], d["stop_name"], d["direction"],
                                d["day_type"], d["departure_time"],
                            )
                            if key not in seen_departures:
                                seen_departures.add(key)
                                all_departures.append(d)

                except Exception as exc:
                    log.warning(
                        f"  PDF page {page.page_number} parse error for {route_id}: {exc}"
                    )
                    # Continue to the next page — one bad page must not drop the rest.

    except Exception as exc:
        log.warning(f"  Failed to open PDF for {route_id}: {exc}")

    log.info(
        f"  {route_id}: {len(all_stops)} stops, "
        f"{len(all_departures)} departure times extracted from PDF."
    )
    return all_stops, all_departures


# ---------------------------------------------------------------------------
# Master orchestration
# ---------------------------------------------------------------------------

def scrape_all() -> dict[str, list[dict]]:
    """
    Full MyCiTi scrape:
      1. Fetch timetable downloads page → extract route list + PDF URLs
      2. For each route, download + parse PDF → stops & departure times

    Returns:
        {routes, stops, timetables, departures}
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    log.info("=== Starting MyCiTi scrape ===")

    # Step 1: get all routes (from timetables page)
    routes = scrape_route_list(session)
    if not routes:
        log.error("No routes found. Check that myciti.org.za is reachable.")
        return {"routes": [], "stops": [], "timetables": [], "departures": []}

    all_stops: list[dict] = []
    all_departures: list[dict] = []
    timetable_meta: list[dict] = []

    # Step 2: download + parse each PDF
    total = len(routes)
    for i, route in enumerate(routes, start=1):
        pdf_url = route["detail_url"]
        log.info(f"[{i}/{total}] {route['route_id']} — {route['route_description']}")

        timetable_meta.append({
            "route_id": route["route_id"],
            "route_name": route["route_name"],
            "day_type": "all",
            "timetable_url": pdf_url,
            "scraped_at": route["scraped_at"],
        })

        pdf_bytes = _get_pdf_bytes(pdf_url, session)
        if pdf_bytes is None:
            log.warning(f"  Skipping {route['route_id']} — PDF unavailable.")
            continue

        stops, departures = parse_pdf_timetable(route["route_id"], pdf_bytes)
        all_stops.extend(stops)
        all_departures.extend(departures)

    log.info(
        f"=== Scrape complete: {len(routes)} routes, "
        f"{len(all_stops)} stops, "
        f"{len(all_departures)} departure times ==="
    )

    return {
        "routes": routes,
        "stops": all_stops,
        "timetables": timetable_meta,
        "departures": all_departures,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _demo() -> None:
    """Quick standalone test: scrape the route list and parse two PDFs."""
    session = requests.Session()
    session.headers.update(HEADERS)

    log.info("Test mode: scraping first 2 routes only …")
    routes = scrape_route_list(session)
    print(f"\nAll routes found ({len(routes)}):")
    for r in routes:
        print(f"  {r['route_id']:<8} {r['route_description']}")

    print("\nParsing PDFs for first 2 routes …")
    for route in routes[:2]:
        pdf_bytes = _get_pdf_bytes(route["detail_url"], session)
        if pdf_bytes:
            stops, deps = parse_pdf_timetable(route["route_id"], pdf_bytes)
            print(f"\n  {route['route_id']} stops: {[s['stop_name'] for s in stops]}")
            print(f"  {route['route_id']} departures (first 5):")
            for d in deps[:5]:
                print(f"    {d['day_type']:<10} {d['stop_name']:<30} {d['departure_time']}")


if __name__ == "__main__":
    _demo()
