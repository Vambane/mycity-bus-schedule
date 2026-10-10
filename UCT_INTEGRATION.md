# UCT Shuttle Integration

Technical details for the UCT shuttle timetable integration. For general
project information, see [README.md](README.md).

## Overview

The UCT shuttle service operates 15 routes across the University of Cape Town
campuses. Timetable data is scraped from the university website and loaded
alongside MyCiTi data into the shared DuckDB database. Both operators coexist
in the same tables, distinguished by the `operator` column (`'myciti'` or
`'uct'`).

**Source**: https://uct.ac.za/students/services-transport-parking-uct-shuttle/route-maps-timetables

## Routes parsed

| Route ID | Name | Day types |
|----------|------|-----------|
| UCT1 | Claremont | weekday, sunday |
| UCT2 | Forest Hill | weekday, sunday |
| UCT3 | Mowbray | weekday, sunday |
| UCT4 | Liesbeeck via Mowbray | weekday |
| UCT5 | Great Westerford | weekday, sunday |
| UCT6 | Carinus | weekday, sunday |
| UCT7 | Sandown | weekday, sunday |
| UCT9 | Obz Square via PNP Rondebosch | weekday, sunday |
| UCT10 | Hiddingh Hall | weekday |
| UCT11 | Obz Square via Faculty of Health Sciences & Tugwell | weekday |
| UCT13 | Residence loops | weekday, sunday |
| UCT14 | Residence loops | weekday, sunday |
| UCT15 | Residence loops | saturday |
| UCT16 | Residence loops | saturday |
| UCT20 | Educare | weekday |

Routes 8, 11, 12 do not appear on the current index page.

## Row counts

| Table | MyCiTi | UCT | Total |
|-------|--------|-----|-------|
| routes | 47 | 15 | 62 |
| stops | ~520 | 23 | ~543 |
| departures | ~171k | ~1,335 | ~172k |

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

Times are newline-delimited within each cell. Some cells contain "No service"
or "Break in service" markers, which are skipped.

### Combined PDFs

Routes 13 & 14 share a single PDF, as do routes 15 & 16. The parser uses
"Route N:" text position markers in the PDF to assign tables to the correct
route by vertical position.

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
table. The app shows a warning banner when `MAX(valid_until)` for the UCT
operator is in the past.

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

43 tests in `tests/test_uct_integration.py` cover:

- Scraper helpers (time normalisation, stop name cleaning, day-type
  classification, validity date parsing)
- Operator-scoped direct and transfer connection searches
- UCT route category and color palette assignment
- Schema migration (DDL + ALTER statements)
- Stale timetable detection (expired/future/missing)
