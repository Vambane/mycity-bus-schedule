# One module per data source keeps the UCT parsing rules in one place.
# pylint: disable=too-many-lines
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
from collections import Counter, defaultdict
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

# Same, without a year (e.g. "Overnight ... (26 Oct - 20 Nov)"); the year is
# taken from the set heading ("Routes valid from 26 October to 20 November 2026").
VALIDITY_NOYEAR_RE = re.compile(
    r"(\d{1,2})\s+([A-Za-z]+)\.?\s*[-–]\s*(\d{1,2})\s+([A-Za-z]+)\.?\s*\)",
)

# Set heading, e.g. "Routes valid from 26 October to 20 November 2026"
SET_RE = re.compile(
    r"valid from\s+(\d{1,2})\s+([A-Za-z]+)\s+to\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})",
    re.I,
)

# Route id used for UCT's Overnight library loop (no route number on the page)
OVERNIGHT_ROUTE_NUM = "OVN"

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


def _parse_set_heading(text: str) -> tuple[Optional[str], Optional[str]]:
    """'Routes valid from 26 October to 20 November 2026' → ISO dates."""
    m = SET_RE.search(text)
    if not m:
        return None, None
    d1, m1, d2, m2, year = m.groups()
    a, b = MONTH_MAP.get(m1.lower()), MONTH_MAP.get(m2.lower())
    if not a or not b:
        return None, None
    return f"{year}-{a:02d}-{int(d1):02d}", f"{year}-{b:02d}-{int(d2):02d}"


def _parse_validity_with_year(
    text: str, year: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Like _parse_validity_dates, but accepts a heading without a year."""
    vf, vu = _parse_validity_dates(text)
    if vf or not year:
        return vf, vu
    m = VALIDITY_NOYEAR_RE.search(text)
    if not m:
        return None, None
    d1, m1, d2, m2 = m.groups()
    a, b = MONTH_MAP.get(m1.lower()), MONTH_MAP.get(m2.lower())
    if not a or not b:
        return None, None
    return f"{year}-{a:02d}-{int(d1):02d}", f"{year}-{b:02d}-{int(d2):02d}"


def select_timetable_set(html: str, today: Optional[str] = None) -> Optional[str]:
    """Pick the set heading to load: the one covering *today*, else the newest.

    Returns the h2 text (e.g. "Routes valid from 26 October to 20 November
    2026"), or None if the page has no dated sets.
    """
    soup = BeautifulSoup(html, "lxml")
    today = today or datetime.now(timezone.utc).date().isoformat()
    sets = []
    for h2 in soup.find_all("h2"):
        t = h2.get_text(" ", strip=True)
        vf, vu = _parse_set_heading(t)
        if vf:
            sets.append((vf, vu, t))
    if not sets:
        return None
    current = [s for s in sets if s[0] <= today <= s[1]]
    return (current or sorted(sets))[-1][2]


# Walks the source layout step by step; splitting it would scatter the parsing rules.
def discover_routes(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    html: str,
    inconsistencies: list[dict],
    timetable_set: Optional[str] = None,
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

    set_year: Optional[str] = None
    if timetable_set:
        _sf, _su = _parse_set_heading(timetable_set)
        set_year = _su[:4] if _su else None

    for h3_a in h3_links:
        heading_text = h3_a.get_text(strip=True)
        href = h3_a["href"]

        # Only load routes from the chosen dated set (the page lists the
        # previous set too).
        if timetable_set:
            h2 = h3_a.find_previous("h2")
            if not h2 or h2.get_text(" ", strip=True) != timetable_set:
                continue

        # Resolve relative URLs
        pdf_url = urljoin(BASE_URL, href)

        # Extract route number(s) from heading
        num_match = ROUTE_NUM_RE.search(heading_text)
        if not num_match and "overnight" in heading_text.lower():
            route_nums_raw = OVERNIGHT_ROUTE_NUM
        elif not num_match:
            log.warning(f"  Could not extract route number from: {heading_text}")
            continue
        else:
            route_nums_raw = num_match.group(1)
        # Handle "13 and 14" → ["13", "14"]
        route_nums = re.split(r"\s*(?:and|&)\s*", route_nums_raw)

        # Extract route description (everything after "Route N:")
        desc_match = re.search(r"Route[s]?\s+[\d\s]+(?:and\s+\d+)?:\s*(.+?)(?:\(|$)",
                               heading_text)
        description = desc_match.group(1).strip() if desc_match else heading_text
        if route_nums_raw == OVERNIGHT_ROUTE_NUM:
            description = "Overnight library loop"

        # Extract validity dates from the heading (year from the set if absent)
        valid_from, valid_until = _parse_validity_with_year(heading_text, set_year)

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
                if img_dates not in ((None, None), heading_dates):
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
            for stop, t in trip.items():
                used[stop].add(t)
            trips.append(trip)

    return trips


def _to_minutes(hms: str) -> int:
    """'HH:MM:SS' → minutes since midnight."""
    h, m, _ = hms.split(":")
    return int(h) * 60 + int(m)


# Walks the source layout step by step; splitting it would scatter the parsing rules.
def _parse_uct_table(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals,too-many-branches,unused-argument
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
        for stop_name, dep_time in trip.items():
            departures.append({
                "route_id": route_id,
                "stop_name": stop_name,
                "direction": direction,
                "day_type": day_type,
                "departure_time": dep_time,
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


# Day-type phrases in PDF text → the app's day types. "Weekend" sections run
# on both Saturday and Sunday, so they are loaded for both.
DAY_PHRASES = [
    (re.compile(
        r"Saturday\s*(?:&|and)\s*Sunday"
        r"|Weekends?(?:\s*(?:&|and)\s*Public\s*Holidays)?",
        re.I,
    ), ("saturday", "sunday")),
    (re.compile(r"\bSaturdays?\b", re.I), ("saturday",)),
    (re.compile(r"\bSundays?\b", re.I), ("sunday",)),
    (re.compile(r"Monday\s*(?:[-–]|to)\s*Friday", re.I), ("weekday",)),
]

# Row labels that are not stops (Overnight loop: "Anticlock" / "Clockwise")
LABEL_CELLS = {"clockwise", "anticlock", "anticlockwise", "anti-clockwise",
               "anti clockwise"}


def _lines(words: list[dict], tol: float = 3.0) -> list[list[dict]]:
    """Group pdfplumber words into lines (sorted top→bottom, left→right)."""
    out: list[list[dict]] = []
    for w in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if out and abs(out[-1][0]["top"] - w["top"]) < tol:
            out[-1].append(w)
        else:
            out.append([w])
    return [sorted(l, key=lambda w: w["x0"]) for l in out]


def _section_markers(page) -> tuple[list[tuple], list[tuple]]:
    """Day-type and route markers with positions.

    Returns (day_markers, route_markers) where each day marker is
    (top, x0, x1, day_types) and each route marker is (top, x0, x1, num).
    Side-by-side sections on one line keep their own x ranges.
    """
    day, route = [], []
    for line in _lines(page.extract_words()):
        text, spans = "", []
        for w in line:
            if text:
                text += " "
            spans.append((len(text), len(text) + len(w["text"]), w))
            text += w["text"]


        taken: list[tuple[int, int]] = []
        for pat, dts in DAY_PHRASES:
            for m in pat.finditer(text):
                if any(m.start() < b and m.end() > a for a, b in taken):
                    continue
                taken.append((m.start(), m.end()))
                x0, x1 = _span_x(spans, m.start(), m.end())
                day.append((line[0]["top"], x0, x1, dts))
        for m in re.finditer(r"Route\s+(\d+)\s*:", text, re.I):
            x0, x1 = _span_x(spans, m.start(), m.end())
            route.append((line[0]["top"], x0, x1, m.group(1)))
    return day, route


def _span_x(spans: list[tuple], a: int, b: int) -> tuple[float, float]:
    """x0/x1 of the words covering characters [a, b) of a joined text line."""
    ws = [w for (s0, s1, w) in spans if s1 > a and s0 < b]
    return ws[0]["x0"], ws[-1]["x1"]


def _nearest_above(markers: list[tuple], top: float, x0: float, x1: float):
    """Closest marker above *top* whose x range overlaps [x0, x1].

    On a tie in height the later marker wins (reversed() keeps that rule,
    since max() returns the first maximum it meets).
    """
    candidates = [m for m in markers if m[0] <= top + 1 and m[1] <= x1 and m[2] >= x0 - 40]
    return max(reversed(candidates), key=lambda m: m[0], default=None)


# Walks the source layout step by step; splitting it would scatter the parsing rules.
def _table_trips(page, ptable) -> list[tuple[list[str], list[list[Optional[str]]]]]:  # pylint: disable=too-many-branches
    """Read one pdfplumber table as [(stop_names, rows_of_times)].

    Rows are taken from word positions, so each printed row is one trip,
    blanks and "No service" cells stay in their own column, and a header
    row that repeats inside the table (Overnight loop) starts a new block.
    """
    raw = ptable.extract()
    if not raw or len(raw) < 2:
        return []
    cells = ptable.rows[0].cells
    cols = [(c[0], c[2]) if c else None for c in cells]
    blocks: list[tuple[list[str], list[list[Optional[str]]]]] = []

    def col_of(xc):
        for i, c in enumerate(cols):
            if c and c[0] - 1 <= xc <= c[1] + 1:
                return i
        return None

    # Header rows come from the table grid itself (reliable, handles wrapped
    # names); word positions are only used for the time rows. A table may
    # repeat its header (Overnight loop), which starts a new block.
    def is_header(r):
        cells_ = [str(c).strip() for c in r if c and str(c).strip()]
        return len(cells_) >= 2 and not any(
            _normalise_time(x.split("\n")[0]) for x in cells_)
    headers = [[(str(c) if c else None) for c in r] for r in raw if is_header(r)]
    if not headers:
        return []
    known = [{" ".join(str(c).split()).lower() for c in h if c} for h in headers]

    hi = 0
    names = headers[0]
    rows: list[list[Optional[str]]] = []
    body = page.crop(ptable.bbox)
    for line in _lines(body.extract_words()):
        times: dict[int, str] = {}
        labels: dict[int, list[str]] = {}
        for w in line:
            ci = col_of((w["x0"] + w["x1"]) / 2)
            if ci is None:
                continue
            t = _normalise_time(w["text"])
            if t:
                times[ci] = t
            else:
                labels.setdefault(ci, []).append(w["text"])
        if times:
            rows.append([times.get(i) for i in range(len(cols))])
            continue
        # A repeated header line (not notes such as "Break in service")
        if rows and hi + 1 < len(headers):
            nxt = known[hi + 1]
            hits = sum(1 for ws in labels.values()
                       if any(k.startswith(" ".join(ws).lower()) for k in nxt))
            if hits >= 2:
                blocks.append((names, rows))
                rows = []
                hi += 1
                names = headers[hi]
    if rows:
        blocks.append((names, rows))

    out = []
    for names, rows in blocks:
        clean = [_clean_stop_name(n) if n else None for n in names]
        clean = [None if (c and c.lower() in LABEL_CELLS) else c for c in clean]
        keep = [i for i, c in enumerate(clean) if c]
        if len(keep) < 2:
            continue
        out.append(([clean[i] for i in keep], [[r[i] for i in keep] for r in rows]))
    return out


def _split_repeats(stops: list[str], rows: list[list[Optional[str]]]):
    """Split side-by-side copies of the same stop list into separate tables."""
    if len(stops) < 4 or stops[0] not in stops[1:]:
        return [(stops, rows)]
    k = stops.index(stops[0], 1)
    if stops[:k] != stops[k:2 * k]:
        return [(stops, rows)]
    left = (stops[:k], [r[:k] for r in rows])
    return [left] + _split_repeats(stops[k:], [r[k:] for r in rows])


# Compliance: where the index page and the PDF disagree we use the earlier
# date. UCT's page says Route 5 runs to 26 Nov 2026; its PDF says 20 Nov.
# Extend this if UCT confirms 26 Nov.
VALID_UNTIL_OVERRIDES = {"UCT5": "2026-11-20"}

# A trip that runs past midnight is printed with its clock times (23:50,
# then 00:05). Times are stored on one service day, so after-midnight
# stops are written past 24:00 (00:05 -> 24:05), the usual timetable
# convention, which keeps every trip's stops in order. Only a drop of at
# least this much counts as midnight; a smaller drop is left alone (it is
# more likely a misprint than a trip running backwards in time).
MIDNIGHT_DROP_MIN = 12 * 60


def crosses_midnight(row) -> bool:
    """True if a trip's times go backwards past midnight (e.g. 23:50 then 00:05)."""
    times = [t for t in row if t]
    return any(
        _to_minutes(a) - _to_minutes(b) >= MIDNIGHT_DROP_MIN
        for a, b in zip(times, times[1:])
    )


def unroll_midnight(row: list[Optional[str]]) -> list[Optional[str]]:
    """Rewrite after-midnight times on one trip as 24:xx so they stay in order.

    ['23:15:00', '23:50:00', '00:05:00'] -> ['23:15:00', '23:50:00', '24:05:00']
    Empty cells (stops the trip skips) stay empty.
    """
    out: list[Optional[str]] = []
    offset = 0
    prev: Optional[int] = None
    for t in row:
        if not t:
            out.append(t)
            continue
        minutes = _to_minutes(t) + offset
        if prev is not None and prev - minutes >= MIDNIGHT_DROP_MIN:
            offset += 24 * 60
            minutes += 24 * 60
        prev = minutes
        h, m = divmod(minutes, 60)
        out.append(f"{h:02d}:{m:02d}:{t[6:8] or '00'}")
    return out


# Walks the source layout step by step; splitting it would scatter the parsing rules.
def parse_uct_pdf(  # pylint: disable=too-many-locals,too-many-branches,too-many-nested-blocks,unused-argument
    route_id: str,
    route_num: str,
    pdf_bytes: bytes,
    day_type_hint: Optional[str],
    inconsistencies: list[dict],
) -> tuple[list[dict], list[dict], Optional[str], Optional[str]]:
    """Parse a UCT shuttle timetable PDF.

    Each printed row is one trip. Tables are assigned to a route and day
    type by the nearest "Route N:" / day heading above them that overlaps
    them horizontally (several tables can sit side by side). "Weekend"
    sections are loaded for both Saturday and Sunday. Trips may skip stops
    ("No service" or blank cells).

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
    hint_days = (("saturday", "sunday") if day_type_hint == "weekend"
                 else (day_type_hint,) if day_type_hint else ("weekday",))
    direction_counter = 0

    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                try:
                    page_text = page.extract_text() or ""
                    vf, vu = _extract_validity_from_pdf(page_text)
                    if vf and vu:
                        valid_from, valid_until = vf, vu
                    day_markers, route_markers = _section_markers(page)

                    for ptable in page.find_tables() or []:
                        x0, top, x1, _ = ptable.bbox
                        dm = _nearest_above(day_markers, top, x0, x1)
                        days = dm[3] if dm else hint_days
                        rm = _nearest_above(route_markers, top, 0, page.width)
                        target_id = f"UCT{rm[3]}" if rm else route_id
                        if target_id != route_id:
                            continue  # combined PDF: another route's table

                        for stops, rows in (
                            part for blk in _table_trips(page, ptable)
                            for part in _split_repeats(*blk)
                        ):
                            direction_counter += 1
                            direction = f"direction_{direction_counter}"
                            for seq, name in enumerate(stops):
                                key = f"{route_id}_{name}_{direction}"
                                if key not in seen_stops:
                                    seen_stops.add(key)
                                    all_stops.append({
                                        "stop_id": f"{route_id}_{direction}_{seq:03d}",
                                        "stop_name": name, "route_id": route_id,
                                        "stop_sequence": seq, "direction": direction,
                                        "stop_lat": None, "stop_lon": None,
                                        "operator": "uct", "scraped_at": now,
                                    })
                            for row in rows:
                                if crosses_midnight(row):
                                    row = unroll_midnight(row)
                                    log.info(f"  {route_id}: trip runs past midnight {row}")
                                for name, t in zip(stops, row):
                                    if not t:
                                        continue
                                    for day in days:
                                        k = (route_id, name, direction, day, t)
                                        if k in seen_departures:
                                            continue
                                        seen_departures.add(k)
                                        all_departures.append({
                                            "route_id": route_id, "stop_name": name,
                                            "direction": direction, "day_type": day,
                                            "departure_time": t, "operator": "uct",
                                            "scraped_at": now,
                                        })
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

# Walks the source layout step by step; splitting it would scatter the parsing rules.
def scrape_all(offline: bool = False) -> dict[str, list[dict]]:  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
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
    timetable_set = select_timetable_set(index_html)
    log.info(f"Timetable set: {timetable_set}")
    routes = discover_routes(index_html, inconsistencies, timetable_set)
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
                # The index-page heading is authoritative (it carries
                # per-route exceptions such as Route 5 → 26 Nov); fall back
                # to dates printed in the PDF.
                "valid_from": route.get("valid_from") or vf,
                "valid_until": VALID_UNTIL_OVERRIDES.get(
                    route_id, route.get("valid_until") or vu),
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
