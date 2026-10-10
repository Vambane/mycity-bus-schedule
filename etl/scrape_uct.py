"""
etl/scrape_uct.py — UCT Shuttle Timetable Scraper
===================================================
Scrapes UCT shuttle route timetables from the university website.

The timetable pages serve PDF files (Excel-to-PDF exports). Each PDF
contains one or more tables in a 2-row format:

    Row 0: stop names (column headers)
    Row 1: newline-delimited departure times per stop

Multiple tables on one page represent different time blocks, directions,
or day-type sections within the same route.

Two operating modes:
  - **Live**: fetch the index page and PDFs from uct.ac.za (rate-limited,
    with retries and robots.txt check).
  - **Offline**: parse previously-saved PDFs from data/uct_raw/.

Raw PDFs are cached to data/uct_raw/ during live fetches so subsequent
runs can work offline.

Run standalone:
    python3 etl/scrape_uct.py              # live fetch
    python3 etl/scrape_uct.py --offline    # parse from cache
"""

import io
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import pdfplumber
import requests
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

BASE_URL = "https://uct.ac.za"
INDEX_URL = (
    f"{BASE_URL}/students/services-transport-parking-uct-shuttle"
    "/route-maps-timetables"
)

HEADERS = {
    "User-Agent": "UCTShuttleScraper/1.0 (academic timetable project)",
    "Accept-Language": "en-ZA,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CACHE_DIR = Path(__file__).parent.parent / "data" / "uct_raw"

# Polite delay between requests (seconds)
REQUEST_DELAY = 1.5

# Regex: HH:MM time (no seconds in UCT timetables)
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")

# Regex: extract validity dates from title, e.g.
# "Core Service timetable : 14 Sept - 25 Oct 2026"
VALIDITY_RE = re.compile(
    r"(\d{1,2})\s+(\w+)\s*[-–]\s*(\d{1,2})\s+(\w+)\s+(\d{4})",
)

# Month name → number lookup (abbreviated and full)
MONTH_MAP = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

# Regex: route number(s) from heading text
ROUTE_NUM_RE = re.compile(r"Route[s]?\s+([\d]+(?:\s*(?:and|&)\s*\d+)?)", re.I)

# Day-type patterns in PDF page text
DAY_PATTERNS = [
    (re.compile(r"Saturday\s*&\s*Sunday|Weekend\s*&\s*Public\s*Holidays", re.I),
     "sunday"),
    (re.compile(r"\bSaturday\b", re.I), "saturday"),
    (re.compile(r"Monday\s*[-–]\s*Friday", re.I), "weekday"),
]

# Cells that are not real departure times
SKIP_CELLS = {"no service", "break in service", ""}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _check_robots(session: requests.Session) -> bool:
    """Check robots.txt before scraping. Returns True if allowed."""
    try:
        rp = RobotFileParser()
        rp.set_url(f"{BASE_URL}/robots.txt")
        resp = session.get(f"{BASE_URL}/robots.txt", headers=HEADERS, timeout=10)
        rp.parse(resp.text.splitlines())
        allowed = rp.can_fetch(HEADERS["User-Agent"], INDEX_URL)
        if not allowed:
            log.warning("robots.txt disallows scraping the shuttle page.")
        return allowed
    except Exception as exc:
        log.warning(f"Could not check robots.txt: {exc}. Proceeding cautiously.")
        return True


def _fetch_url(
    url: str,
    session: requests.Session,
    retries: int = 3,
) -> Optional[bytes]:
    """Fetch a URL with retries and exponential backoff."""
    for attempt in range(1, retries + 1):
        try:
            log.info(f"GET {url}")
            r = session.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            time.sleep(REQUEST_DELAY)
            return r.content
        except requests.RequestException as exc:
            log.warning(f"  Attempt {attempt}/{retries} failed ({url}): {exc}")
            if attempt < retries:
                time.sleep(REQUEST_DELAY * (2 ** attempt))
    return None


# ---------------------------------------------------------------------------
# Index page parsing — route discovery
# ---------------------------------------------------------------------------

def _parse_validity_dates(text: str) -> tuple[Optional[str], Optional[str]]:
    """Extract valid_from and valid_until dates from a title string.

    E.g. "Route 1: Claremont (14 Sept – 25 Oct 2026)"
    → ('2026-09-14', '2026-10-25')

    Returns (None, None) if the pattern doesn't match.
    """
    m = VALIDITY_RE.search(text)
    if not m:
        return None, None
    day_from, mon_from, day_to, mon_to, year = m.groups()
    m_from = MONTH_MAP.get(mon_from.lower().rstrip("."))
    m_to = MONTH_MAP.get(mon_to.lower().rstrip("."))
    if m_from is None or m_to is None:
        log.warning(f"  Unrecognised month in '{text}': {mon_from}/{mon_to}")
        return None, None
    return (
        f"{year}-{m_from:02d}-{int(day_from):02d}",
        f"{year}-{m_to:02d}-{int(day_to):02d}",
    )


def discover_routes(
    html: str,
    inconsistencies: list[dict],
) -> list[dict]:
    """Parse the UCT shuttle index page and return route metadata.

    Each entry in the returned list has:
        route_id, route_name, route_description, detail_url,
        valid_from, valid_until, pdf_urls, day_type_hints

    Routes 13&14 have two separate PDF links (weekday + weekend).
    """
    soup = BeautifulSoup(html, "lxml")
    now = datetime.now(timezone.utc).isoformat()
    routes: list[dict] = []

    # Find all h3 links that point to /media/ (timetable PDFs)
    h3_links = soup.select("h3 a[href*='/media/']")

    for h3_a in h3_links:
        heading_text = h3_a.get_text(strip=True)
        href = h3_a["href"]

        # Resolve relative URLs
        pdf_url = urljoin(BASE_URL, href)

        # Extract route number(s) from heading
        num_match = ROUTE_NUM_RE.search(heading_text)
        if not num_match:
            log.warning(f"  Could not extract route number from: {heading_text}")
            continue

        route_nums_raw = num_match.group(1)
        # Handle "13 and 14" → ["13", "14"]
        route_nums = re.split(r"\s*(?:and|&)\s*", route_nums_raw)

        # Extract route description (everything after "Route N:")
        desc_match = re.search(r"Route[s]?\s+[\d\s]+(?:and\s+\d+)?:\s*(.+?)(?:\(|$)",
                               heading_text)
        description = desc_match.group(1).strip() if desc_match else heading_text

        # Extract validity dates from the heading
        valid_from, valid_until = _parse_validity_dates(heading_text)

        # Collect ALL PDF links for this route group — look at sibling
        # divs after the h3 for additional timetable links.
        # The h3 link itself is one PDF. Additional links labelled
        # "Weekday timetable" or "Weekend timetable" may follow.
        pdf_entries: list[dict] = [{"url": pdf_url, "hint": None}]

        # Walk siblings of the h3's parent to find additional /media/ links
        h3_elem = h3_a.parent  # the <h3>
        parent_container = h3_elem.parent if h3_elem else None
        if parent_container:
            # Find all links in the same container that are NOT the h3 link
            for a in parent_container.find_all("a", href=True):
                a_href = a["href"]
                if "/media/" not in a_href:
                    continue
                a_url = urljoin(BASE_URL, a_href)
                if a_url == pdf_url:
                    continue  # already captured
                a_text = a.get_text(strip=True).lower()
                # Classify the link: weekday or weekend hint
                hint = None
                if "weekday" in a_text:
                    hint = "weekday"
                elif "weekend" in a_text:
                    hint = "weekend"
                elif "timetable" in a_text:
                    hint = None  # generic
                elif "map" in a_text:
                    continue  # skip route map links
                pdf_entries.append({"url": a_url, "hint": hint})

        # Detect the Clarinus/Carinus inconsistency: check if the heading
        # says one spelling but the image alt text says another.
        if parent_container:
            img = parent_container.find("img", alt=True)
            if img:
                img_alt = img["alt"]
                # Check for spelling mismatch
                heading_lower = heading_text.lower()
                img_lower = img_alt.lower()
                if ("clarinus" in heading_lower) != ("clarinus" in img_lower):
                    inconsistencies.append({
                        "type": "spelling_mismatch",
                        "route": route_nums_raw,
                        "heading": heading_text,
                        "img_alt": img_alt,
                        "detail": (
                            f"Heading uses "
                            f"{'Clarinus' if 'clarinus' in heading_lower else 'Carinus'}"
                            f" but image alt uses "
                            f"{'Clarinus' if 'clarinus' in img_lower else 'Carinus'}"
                        ),
                    })
                # Check for date range mismatch between heading and img alt
                img_dates = _parse_validity_dates(img_alt)
                heading_dates = (valid_from, valid_until)
                if img_dates != (None, None) and img_dates != heading_dates:
                    inconsistencies.append({
                        "type": "date_range_mismatch",
                        "route": route_nums_raw,
                        "heading_dates": heading_dates,
                        "img_dates": img_dates,
                        "detail": (
                            f"Heading says {heading_dates}, "
                            f"image alt says {img_dates}"
                        ),
                    })

        # Build route entries — one per route number for combined routes
        # (e.g. "Routes 13 and 14" → two entries sharing the same PDFs)
        for rn in route_nums:
            route_id = f"UCT{rn.strip()}"
            routes.append({
                "route_id": route_id,
                "route_name": f"UCT {rn.strip()} – {description}",
                "route_description": description,
                "detail_url": pdf_url,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "operator": "uct",
                "scraped_at": now,
                # Internal: used during PDF fetch, not stored in DB
                "_pdf_entries": pdf_entries,
                "_route_num": rn.strip(),
            })

    log.info(f"Discovered {len(routes)} UCT routes from index page.")
    return routes


# ---------------------------------------------------------------------------
# PDF timetable parsing
# ---------------------------------------------------------------------------

def _normalise_time(raw: str) -> Optional[str]:
    """Convert 'HH:MM' → 'HH:MM:SS'. Returns None if not a valid time."""
    raw = raw.strip().replace(".", ":")
    if TIME_RE.match(raw):
        h, m = raw.split(":")
        if int(h) < 30 and int(m) < 60:
            return f"{int(h):02d}:{m}:00"
    return None


def _clean_stop_name(raw: str) -> Optional[str]:
    """Normalise a stop-name cell from the PDF.

    UCT PDFs have stop names in the header row. Multi-word names can
    span lines (e.g. 'Roscommon\\nHouse', 'Faculty of\\nHealth Sciences').
    Collapse whitespace and reject cells that are times or junk.
    """
    name = " ".join(raw.split()).strip()
    if not name or name.lower() in SKIP_CELLS or _normalise_time(name):
        return None
    # Reject cells that look like instructional text (long sentences)
    if len(name) > 50:
        return None
    return name


def _classify_day_type(text: str) -> Optional[str]:
    """Detect a day-type from free text. Returns weekday/saturday/sunday or None."""
    for pattern, day_type in DAY_PATTERNS:
        if pattern.search(text):
            return day_type
    return None


def _extract_text_markers(
    page: "pdfplumber.page.Page",
) -> tuple[list[tuple[float, str]], list[tuple[float, str]]]:
    """Extract day-type and route markers with y-positions from a PDF page.

    Scans the page's extracted words to find day-type headers
    ("Monday – Friday", "Weekend & Public Holidays", "Saturday") and
    route headers ("Route N:"), returning their vertical positions so
    each table can be assigned to the correct section.

    Returns:
        (day_markers, route_markers) — each a sorted list of (y, value).
    """
    day_markers: list[tuple[float, str]] = []
    route_markers: list[tuple[float, str]] = []

    # Group words into lines by y-position (within 3pt tolerance)
    words = page.extract_words()
    if not words:
        return day_markers, route_markers

    # Build lines: group consecutive words within ±3pt of the same y
    text_lines: list[tuple[float, str]] = []
    current_y = words[0]["top"]
    current_text = words[0]["text"]
    for w in words[1:]:
        if abs(w["top"] - current_y) < 3:
            current_text += " " + w["text"]
        else:
            text_lines.append((current_y, current_text))
            current_y = w["top"]
            current_text = w["text"]
    text_lines.append((current_y, current_text))

    for y, line in text_lines:
        dt = _classify_day_type(line)
        if dt:
            day_markers.append((y, dt))
        rm = re.search(r"Route\s+(\d+)\s*:", line, re.I)
        if rm:
            route_markers.append((y, rm.group(1)))

    return day_markers, route_markers


def _extract_validity_from_pdf(text: str) -> tuple[Optional[str], Optional[str]]:
    """Extract validity dates from PDF page text."""
    return _parse_validity_dates(text)


def _reconstruct_trips(
    stop_times: dict[str, list[str]],
    stop_order: list[str],
    max_leg_min: int = 60,
) -> list[dict[str, str]]:
    """Reconstruct trips from independent stop time lists.

    UCT PDFs list times per stop without trip associations. This function
    infers which times belong to the same trip by matching times that form
    a sensible progression across stops.

    Args:
        stop_times: {stop_name: [sorted times]}
        stop_order: Stops in left-to-right table order (journey sequence)
        max_leg_min: Max minutes between consecutive stops on one trip

    Returns:
        List of trips, each a dict {stop_name: departure_time}
    """
    if not stop_order or not stop_times:
        return []

    # Track which times have been assigned to trips
    used: dict[str, set[str]] = {stop: set() for stop in stop_order}
    trips: list[dict[str, str]] = []

    # Start from the first stop and greedily build trips
    first_stop = stop_order[0]
    for start_time in stop_times.get(first_stop, []):
        if start_time in used[first_stop]:
            continue

        # Try to build a complete trip starting at this time
        trip: dict[str, str] = {first_stop: start_time}
        current_time = start_time
        valid = True

        for i in range(1, len(stop_order)):
            stop = stop_order[i]
            if stop not in stop_times:
                valid = False
                break

            # Find the earliest unused time after current_time
            candidates = [
                t for t in stop_times[stop]
                if t not in used[stop] and t > current_time
            ]
            if not candidates:
                valid = False
                break

            next_time = min(candidates)

            # Check if the leg duration is reasonable
            delta_min = _to_minutes(next_time) - _to_minutes(current_time)
            if delta_min > max_leg_min:
                valid = False
                break

            trip[stop] = next_time
            current_time = next_time

        # Only accept trips that visit all stops
        if valid and len(trip) == len(stop_order):
            for stop, time in trip.items():
                used[stop].add(time)
            trips.append(trip)

    return trips


def _to_minutes(hms: str) -> int:
    """'HH:MM:SS' → minutes since midnight."""
    h, m, _ = hms.split(":")
    return int(h) * 60 + int(m)


def _parse_uct_table(
    table: list[list[str | None]],
    route_id: str,
    day_type: str,
    direction: str,
    now: str,
    inconsistencies: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Parse a single 2-row UCT timetable table.

    UCT PDF tables are 2 rows:
        Row 0: stop names (one per column, left-to-right = journey order)
        Row 1: newline-delimited times per stop

    Times are reconstructed into trips by matching times that form a
    sensible progression across stops (each stop's time > previous stop).

    Returns (stops_list, departures_list).
    """
    if not table or len(table) < 2:
        return [], []

    # Row 0: stop names (in journey order)
    headers = [str(c).strip() if c else "" for c in table[0]]
    stop_names = [_clean_stop_name(h) for h in headers]

    # If no valid stop names, this is probably a notes table
    valid_stops = [s for s in stop_names if s is not None]
    if len(valid_stops) < 2:
        return [], []

    # Filter to only valid stop names, preserving order
    stop_order = [s for s in stop_names if s is not None]

    # Collect all times per stop
    stop_times: dict[str, list[str]] = {stop: [] for stop in stop_order}

    for row_idx in range(1, len(table)):
        row = table[row_idx]
        if not row:
            continue

        for col_idx, cell in enumerate(row):
            if col_idx >= len(stop_names) or stop_names[col_idx] is None:
                continue
            stop_name = stop_names[col_idx]
            if not cell:
                continue

            cell_str = str(cell).strip()
            for line in cell_str.split("\n"):
                line = line.strip()
                if not line or line.lower() in SKIP_CELLS:
                    continue
                t = _normalise_time(line)
                if t:
                    stop_times[stop_name].append(t)

    # Sort times per stop
    for stop in stop_times:
        stop_times[stop].sort()

    # Reconstruct trips from time lists
    trips = _reconstruct_trips(stop_times, stop_order)

    # Build departures from reconstructed trips
    departures: list[dict] = []
    for trip in trips:
        for stop_name, time in trip.items():
            departures.append({
                "route_id": route_id,
                "stop_name": stop_name,
                "direction": direction,
                "day_type": day_type,
                "departure_time": time,
                "operator": "uct",
                "scraped_at": now,
            })

    # Build stop entries from the header (preserving column order = journey order)
    stops: list[dict] = []
    for seq, name in enumerate(stop_order, start=1):
        stops.append({
            "stop_id": f"{route_id}_{direction}_{seq:03d}",
            "stop_name": name,
            "route_id": route_id,
            "stop_sequence": seq,
            "direction": direction,
            "stop_lat": None,
            "stop_lon": None,
            "operator": "uct",
            "scraped_at": now,
        })

    return stops, departures


def parse_uct_pdf(
    route_id: str,
    route_num: str,
    pdf_bytes: bytes,
    day_type_hint: Optional[str],
    inconsistencies: list[dict],
) -> tuple[list[dict], list[dict], Optional[str], Optional[str]]:
    """Parse a UCT shuttle timetable PDF.

    Args:
        route_id:       e.g. 'UCT1'
        route_num:      e.g. '1' — used to match the right sub-table in
                        combined PDFs (Routes 13&14).
        pdf_bytes:      Raw PDF content.
        day_type_hint:  If the index page labelled this PDF as 'weekday'
                        or 'weekend', use that as the default day type.
        inconsistencies: Mutable list to append data-quality issues to.

    Returns:
        (stops, departures, valid_from, valid_until)
    """
    now = datetime.now(timezone.utc).isoformat()
    all_stops: list[dict] = []
    all_departures: list[dict] = []
    seen_stops: set[str] = set()
    seen_departures: set[tuple] = set()
    valid_from: Optional[str] = None
    valid_until: Optional[str] = None

    # The day type may be set by the PDF text or by the index-page hint
    current_day_type = day_type_hint or "weekday"
    direction_counter = 0

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                try:
                    page_text = page.extract_text() or ""

                    # Extract validity dates from page title
                    vf, vu = _extract_validity_from_pdf(page_text)
                    if vf and vu:
                        valid_from = vf
                        valid_until = vu

                    # Build a y-position map of day-type and route
                    # headers. This lets us assign the correct day type
                    # to each table based on vertical position, handling
                    # pages with both weekday and weekend sections.
                    day_markers: list[tuple[float, str]] = []  # (y, day_type)
                    route_markers: list[tuple[float, str]] = []  # (y, num)

                    for word in page.extract_words():
                        text = word["text"]
                        y = word["top"]
                        # Check for day-type keywords in the word context
                        # We look at individual words, so construct small
                        # phrases by checking nearby words
                        pass

                    # Use chars-based approach: extract text lines with
                    # their y-positions from the page for day-type detection.
                    _day_markers, _route_markers = _extract_text_markers(page)
                    day_markers = _day_markers
                    route_markers = _route_markers

                    # Default day type from hint or first marker
                    default_day = day_type_hint or "weekday"
                    if day_markers:
                        default_day = day_markers[0][1]

                    # Extract tables (with bounding box info)
                    raw_tables = page.find_tables()
                    if not raw_tables:
                        raw_tables = page.find_tables({
                            "vertical_strategy": "text",
                            "horizontal_strategy": "text",
                        })

                    for ptable in (raw_tables or []):
                        table = ptable.extract()
                        if not table or len(table) < 2:
                            continue

                        # Peek: is this a real data table?
                        headers = [str(c).strip() if c else "" for c in table[0]]
                        valid_names = [h for h in headers
                                       if _clean_stop_name(h) is not None]
                        if len(valid_names) < 2:
                            continue

                        # Find the day type for this table based on
                        # its vertical position (top of bounding box)
                        table_top = ptable.bbox[1]  # y0
                        table_day = default_day
                        for marker_y, marker_day in day_markers:
                            if marker_y <= table_top:
                                table_day = marker_day
                            else:
                                break

                        # Find the route for this table
                        target_route = route_num
                        for marker_y, marker_route in route_markers:
                            if marker_y <= table_top:
                                target_route = marker_route
                            else:
                                break
                        target_id = f"UCT{target_route}"

                        direction_counter += 1
                        direction = f"direction_{direction_counter}"

                        page_stops, page_deps = _parse_uct_table(
                            table, target_id, table_day,
                            direction, now, inconsistencies,
                        )

                        if not page_deps:
                            direction_counter -= 1
                            continue

                        for s in page_stops:
                            key = f"{s['route_id']}_{s['stop_name']}_{s['direction']}"
                            if key not in seen_stops:
                                seen_stops.add(key)
                                all_stops.append(s)

                        for d in page_deps:
                            key = (
                                d["route_id"], d["stop_name"],
                                d["direction"], d["day_type"],
                                d["departure_time"],
                            )
                            if key not in seen_departures:
                                seen_departures.add(key)
                                all_departures.append(d)

                except Exception as exc:
                    log.warning(
                        f"  PDF page {page.page_number} parse error "
                        f"for {route_id}: {exc}"
                    )

    except Exception as exc:
        log.warning(f"  Failed to open PDF for {route_id}: {exc}")

    log.info(
        f"  {route_id}: {len(all_stops)} stops, "
        f"{len(all_departures)} departures extracted."
    )
    return all_stops, all_departures, valid_from, valid_until


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scrape_all(offline: bool = False) -> dict[str, list[dict]]:
    """Full UCT shuttle scrape.

    Args:
        offline: If True, parse from cached PDFs in data/uct_raw/
                 instead of fetching from the network.

    Returns:
        {routes, stops, timetables, departures, inconsistencies}
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    inconsistencies: list[dict] = []

    log.info("=== Starting UCT shuttle scrape ===")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # --- Step 1: get the index page ---
    index_html: Optional[str] = None
    index_cache = CACHE_DIR / "index.html"

    if offline:
        if index_cache.exists():
            index_html = index_cache.read_text(encoding="utf-8")
            log.info(f"Loaded cached index page from {index_cache}")
        else:
            log.error(
                f"Offline mode but no cached index at {index_cache}. "
                "Save the page first."
            )
            return _empty_result(inconsistencies)
    else:
        if not _check_robots(session):
            log.warning("Proceeding despite robots.txt check (may fail).")

        content = _fetch_url(INDEX_URL, session)
        if content is None:
            log.error("Failed to fetch index page.")
            return _empty_result(inconsistencies)
        index_html = content.decode("utf-8", errors="replace")
        # Cache for offline use
        index_cache.write_text(index_html, encoding="utf-8")

    # --- Step 2: discover routes ---
    routes = discover_routes(index_html, inconsistencies)
    if not routes:
        log.error("No routes discovered from index page.")
        return _empty_result(inconsistencies)

    # --- Step 3: fetch and parse each PDF ---
    all_stops: list[dict] = []
    all_departures: list[dict] = []
    timetable_meta: list[dict] = []

    # Group routes that share PDF entries (e.g. Routes 13&14)
    processed_pdfs: dict[str, bytes] = {}

    for route in routes:
        route_id = route["route_id"]
        route_num = route["_route_num"]
        pdf_entries = route["_pdf_entries"]

        log.info(f"Processing {route_id} — {route['route_description']}")

        for entry in pdf_entries:
            pdf_url = entry["url"]
            day_type_hint = entry["hint"]

            # Cache key: filename derived from media ID
            media_id = pdf_url.rstrip("/").split("/")[-1]
            cache_file = CACHE_DIR / f"media_{media_id}.pdf"

            # Fetch or load from cache
            if pdf_url in processed_pdfs:
                pdf_bytes = processed_pdfs[pdf_url]
            elif offline and cache_file.exists():
                pdf_bytes = cache_file.read_bytes()
                log.info(f"  Loaded cached PDF: {cache_file.name}")
            elif offline:
                log.warning(
                    f"  Offline mode: missing {cache_file.name} for {route_id}. "
                    f"Save from {pdf_url}"
                )
                continue
            else:
                pdf_bytes_result = _fetch_url(pdf_url, session)
                if pdf_bytes_result is None:
                    log.warning(f"  Failed to fetch PDF for {route_id}: {pdf_url}")
                    continue
                pdf_bytes = pdf_bytes_result
                # Cache the downloaded PDF
                cache_file.write_bytes(pdf_bytes)

            processed_pdfs[pdf_url] = pdf_bytes

            # Parse
            stops, departures, vf, vu = parse_uct_pdf(
                route_id, route_num, pdf_bytes,
                day_type_hint, inconsistencies,
            )
            all_stops.extend(stops)
            all_departures.extend(departures)

            timetable_meta.append({
                "route_id": route_id,
                "route_name": route["route_name"],
                "day_type": day_type_hint or "all",
                "timetable_url": pdf_url,
                "valid_from": vf or route.get("valid_from"),
                "valid_until": vu or route.get("valid_until"),
                "operator": "uct",
                "scraped_at": route["scraped_at"],
            })

    # --- Global deduplication ---
    # Combined PDFs (Routes 13&14, 15&16) are parsed once per route that
    # shares them, producing duplicate stop and departure rows. Dedupe
    # across the full result set by composite key.
    seen_dep_keys: set[tuple] = set()
    deduped_departures: list[dict] = []
    for d in all_departures:
        key = (d["route_id"], d["stop_name"], d["direction"],
               d["day_type"], d["departure_time"])
        if key not in seen_dep_keys:
            seen_dep_keys.add(key)
            deduped_departures.append(d)

    seen_stop_keys: set[tuple] = set()
    deduped_stops: list[dict] = []
    for s in all_stops:
        key = (s["route_id"], s["stop_name"], s["direction"])
        if key not in seen_stop_keys:
            seen_stop_keys.add(key)
            deduped_stops.append(s)

    # Regenerate stop_id to ensure uniqueness after dedup
    # (prevents duplicate IDs when shared PDFs produce overlapping sequences)
    from collections import defaultdict
    seq_counters: dict[tuple[str, str], int] = defaultdict(int)
    for stop in deduped_stops:
        route_dir_key = (stop["route_id"], stop["direction"])
        seq = seq_counters[route_dir_key]
        stop["stop_id"] = f"{stop['route_id']}_{stop['direction']}_{seq:03d}"
        stop["stop_sequence"] = seq
        seq_counters[route_dir_key] += 1

    # Clean internal keys from route dicts before returning
    clean_routes = [
        {k: v for k, v in r.items() if not k.startswith("_")}
        for r in routes
    ]

    # Log inconsistencies
    if inconsistencies:
        log.warning(f"=== {len(inconsistencies)} data inconsistencies found ===")
        for inc in inconsistencies:
            log.warning(f"  [{inc['type']}] Route {inc.get('route', '?')}: "
                        f"{inc['detail']}")

    log.info(
        f"=== UCT scrape complete: {len(clean_routes)} routes, "
        f"{len(deduped_stops)} stops, {len(deduped_departures)} departures "
        f"(before dedup: {len(all_stops)} stops, "
        f"{len(all_departures)} departures) ==="
    )

    return {
        "routes": clean_routes,
        "stops": deduped_stops,
        "timetables": timetable_meta,
        "departures": deduped_departures,
        "inconsistencies": inconsistencies,
    }


def _empty_result(inconsistencies: list[dict]) -> dict[str, list[dict]]:
    """Return an empty result set."""
    return {
        "routes": [],
        "stops": [],
        "timetables": [],
        "departures": [],
        "inconsistencies": inconsistencies,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _demo(offline: bool = False) -> None:
    """Quick standalone test: scrape and summarise."""
    result = scrape_all(offline=offline)

    print(f"\nRoutes discovered: {len(result['routes'])}")
    for r in result["routes"]:
        print(f"  {r['route_id']:<10} {r['route_description']}")

    print(f"\nTotal stops: {len(result['stops'])}")
    print(f"Total departures: {len(result['departures'])}")

    # Per-route departure counts
    from collections import Counter
    route_counts = Counter(d["route_id"] for d in result["departures"])
    print("\nDepartures per route:")
    for rid, cnt in sorted(route_counts.items()):
        print(f"  {rid:<10} {cnt:>5}")

    # Day type breakdown
    day_counts = Counter(d["day_type"] for d in result["departures"])
    print(f"\nBy day type: {dict(day_counts)}")

    # Unique stop names
    stop_names = sorted({d["stop_name"] for d in result["departures"]})
    print(f"\nUnique stops ({len(stop_names)}):")
    for s in stop_names:
        print(f"  {s}")

    if result.get("inconsistencies"):
        print(f"\n=== Inconsistencies ({len(result['inconsistencies'])}) ===")
        for inc in result["inconsistencies"]:
            print(f"  [{inc['type']}] {inc['detail']}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="UCT Shuttle Timetable Scraper")
    parser.add_argument(
        "--offline", action="store_true",
        help="Parse from cached PDFs in data/uct_raw/ instead of fetching.",
    )
    args = parser.parse_args()
    _demo(offline=args.offline)
