# UCT Shuttle Integration

Technical details for the UCT shuttle timetable integration. For general
project information, see [README.md](README.md).

## Overview

The UCT shuttle service operates 15 routes in the current Consolidation & Exam set
(26 Oct – 20 Nov 2026) across the University of Cape Town
campuses. Timetable data is scraped from the university website and loaded
alongside MyCiTi data into the shared DuckDB database. Both operators coexist
in the same tables, distinguished by the `operator` column (`'myciti'` or
`'uct'`).

**Source**: https://uct.ac.za/students/services-transport-parking-uct-shuttle/route-maps-timetables

## Routes parsed

| Route ID | Name | Day types | Valid |
|----------|------|-----------|-------|
| UCT1 | Claremont | weekday, saturday, sunday | 26 Oct – 20 Nov 2026 |
| UCT2 | Forest Hill | weekday | 26 Oct – 20 Nov 2026 |
| UCT3 | Mowbray | weekday | 26 Oct – 20 Nov 2026 |
| UCT4 | Liesbeeck via Mowbray | weekday | 26 Oct – 20 Nov 2026 |
| UCT5 | Forest Hill / Great Westerford | weekday | 26 Oct – 20 Nov 2026 (page says 26 Nov; see below) |
| UCT6 | Carinus | weekday | 26 Oct – 20 Nov 2026 |
| UCT7 | Rochester | weekday | 26 Oct – 20 Nov 2026 |
| UCT9 | Obz Square via PNP Rondebosch | weekday | 26 Oct – 20 Nov 2026 |
| UCT10 | Hiddingh Hall | weekday, saturday, sunday | 26 Oct – 20 Nov 2026 |
| UCT11 | Obz Square via FHS & Tugwell | weekday | 26 Oct – 20 Nov 2026 |
| UCT13 | Residence loop | weekday, saturday, sunday | 26 Oct – 20 Nov 2026 |
| UCT14 | Residence loop | weekday, saturday, sunday | 26 Oct – 20 Nov 2026 |
| UCT15 | Residence half loop | saturday, sunday | 26 Oct – 20 Nov 2026 |
| UCT16 | Residence half loop | saturday, sunday | 26 Oct – 20 Nov 2026 |
| UCTOVN | Overnight library loop | weekday (Mon–Fri) | 26 Oct – 20 Nov 2026 |

Routes 8, 12 and 20 are not in this set. Route 20 (Educare) was dropped:
the loader replaces all UCT rows, so no stale Route 20 times remain.
"Weekend" sections are loaded for both Saturday and Sunday.

The index page lists several timetable sets under `<h2>` headings;
`select_timetable_set()` picks the one covering today (else the newest) and
only routes under that heading are scraped.


## Row counts

| Table | MyCiTi | UCT | Total |
|-------|--------|-----|-------|
| routes | 47 | 15 | 62 |
| stops | ~520 | 23 | ~543 |
| departures | ~167k | ~1,510 | ~168k |

UCT departures break down as: weekday=1,091, sunday=233, saturday=11.

## Scraper architecture (`etl/scrape_uct.py`)

### Modes

- **Live** (`python3 run_etl.py --uct`): fetches the index page and each
  route's PDF from uct.ac.za, with a 1.5 s delay between requests and
  robots.txt check. PDFs are cached to `data/uct_raw/` for offline use.
- **Offline** (`python3 run_etl.py --uct --offline`): parses from cached
  PDFs without network access.

### PDF format

UCT timetable PDFs use a 2-row table layout (transposed from MyCiTi's format):

```
Row 0:  Stop A    | Stop B    | Stop C    | ...
Row 1:  08:00     | 08:10     | 08:20     | ...
        08:30     | 08:40     | 08:50     |
        ...       | ...       | ...       |
```

Stop names come from the table header; times are read row by row from word
positions, so each printed row is one trip. Blank or "No service" cells are
kept as gaps, so partial trips survive. A repeated header starts a new block,
and side-by-side copies of the same stop list are split into separate tables.

### Combined PDFs

Routes 13 & 14 share a single PDF, as do routes 15 & 16. The parser assigns
each table to the nearest "Route N" and day-type marker above it that
overlaps it horizontally, and skips tables belonging to the other route.

### Day-type assignment

Multiple day-type sections (e.g., "Monday - Friday" and "Weekend & Public
Holidays") may appear on a single PDF page. The parser extracts day-type
text markers with their y-coordinates and assigns each table to the nearest
day-type marker above it.

### Deduplication

Combined PDFs are parsed once per route that shares them, which produces
duplicate rows. The `scrape_all()` function performs global deduplication
by composite key `(route_id, stop_name, direction, day_type, departure_time)`.

## Data inconsistencies

Known issues logged during scraping:

- **Clarinus/Carinus**: Route 6 heading says "Clarinus" but the PDF body
  uses "Carinus". The PDF body name is used.
- **Date range mismatches**: Some route heading dates differ from the image
  alt text on the index page. The heading dates are used.

## Stops

23 UCT stops are listed in `data/uct_stops.csv`. All coordinates are currently
null (`verified=false`). Stops are campus-specific names (Bremner, Carinus,
UC North, etc.) with no overlap with MyCiTi stop names.

Until coordinates are added, UCT stops will not appear on the geographic
system map but are fully functional in journey search and departure boards.

## Timetable validity

UCT timetables include validity date ranges (e.g., "14 Sept - 25 Oct 2026"),
stored in the `valid_from` and `valid_until` columns of the `timetables`
table. Validity is checked per route (`fastapi_app/services/uct_validity.py`,
dates in Africa/Johannesburg): once a route's `valid_until` has passed its
times are removed from search results (direct and transfer) and departure
boards, and the "Timetable expired" banner names the expired routes. The
index-page heading dates are used, except that where page and PDF disagree
the earlier date wins: Route 5's page says 26 Nov, its PDF 20 Nov, so it
ends 20 Nov (`VALID_UNTIL_OVERRIDES`); extend if UCT confirms 26 Nov.

### Known parsing limits

- Footnotes are shown as static notes (`fastapi_app/services/uct_notes.py`)
  in search results and departure boards, not applied to the times.
- Trips on Routes 13/14 that cross midnight are left out (known gap; times
  are stored within one day). Post-midnight-only trips (Overnight loop) are
  kept as 00:xx weekday times.
- One-direction PDFs (Routes 4, 5, 6, 7, 9, 11) are kept as printed.

## App integration

### Operator filter

All query endpoints accept an `operator` parameter:

- `both` (default) — show all operators
- `myciti` — MyCiTi routes only
- `uct` — UCT shuttle routes only

The filter is applied to:
- Journey search (direct and transfer connections)
- Stop departure board
- System map network
- Stops API (`/api/stops`)
- Network API (`/api/map/network`)

### Route colors

UCT routes use a distinct navy/teal palette (`UCT_COLORS` in `system_map.py`),
separate from MyCiTi's trunk (red), direct (blue), and area (green) palettes.

### CSS

The `.route-badge.uct` class uses `var(--route-uct)` (#003F5C), defined in
`tokens.css` alongside the existing MyCiTi route color tokens.

## Schema changes

The `operator` column was added to `routes`, `stops`, `timetables`,
`departures`, and `scrape_log` with `DEFAULT 'myciti'`. The `timetables`
table also gained `valid_from DATE` and `valid_until DATE`.

Migration statements in `load_db.py` use `ALTER TABLE ADD COLUMN IF NOT
EXISTS` for idempotent upgrades of existing databases.

## Testing

Tests in `tests/test_uct_integration.py` and `tests/test_uct_validity.py` cover:

- Scraper helpers (time normalisation, stop name cleaning, day-type
  classification, validity date parsing)
- Operator-scoped direct and transfer connection searches
- UCT route category and color palette assignment
- Schema migration (DDL + ALTER statements)
- Stale timetable detection (expired/future/missing)
- Per-route validity: Route 5 ends 20 Nov with the rest, route notes,
  cross-midnight trips absent, banner and hidden times after 20 Nov,
  timetable-set selection and Overnight loop heading parsing
