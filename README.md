# 🚌 MyCiTi Bus Timetable

A Streamlit app for exploring Cape Town's MyCiTi bus network: search any stop,
see its upcoming departures, and browse an interactive map of the whole system
overlaid on the city.

The official MyCiTi site only publishes timetables as PDFs. This project
scrapes those PDFs into a queryable database and adds the tools the site
doesn't have — live "next bus" lookups, a visual departure timeline, and a
clickable geographic system map.

## Features

- **Journey search** — pick a From and To stop (flight-search style, with a
  ⇄ swap button) and get every direct service between them as result cards:
  departure time, arrival time, duration, with Best / Fastest / All-day tabs
  and per-route filters in the sidebar
- **Departure board** — leave "Going to" empty to see every route serving a
  stop and its next 10 departures per route and direction, filtered to the
  current time in Cape Town (weekday / Saturday / Sunday–public holiday
  timetables, defaulting to today's)
- **Departure map** — a timeline chart of every departure of the day per
  route/direction, with a marker at the current time, so frequency, peak
  bunching and the last bus are visible at a glance
- **Interactive system map** — the full network on real Cape Town geography
  (Leaflet + CARTO tiles), with a toggle between:
  - *Schematic*: square-ish 45°/90° lines in the style of the official route map
  - *Street*: the exact road-following route geometries from city open data

  Click a route in the legend to highlight it; click any stop to open its
  timetable. Stops sit at their true coordinates (~96% geolocated).
- **Load shedding awareness**: a sidebar panel shows the current Eskom
  stage (live via EskomSePush, manually overridable, defaulting to stage 0)

## How it works

```
myciti.org.za route-timetable PDFs
        │  scraped + parsed (requests, pdfplumber)
        ▼
data/myciti.duckdb  ◄── City of Cape Town open data (stop coordinates,
        │               street route geometries: data/cct_*.geojson)
        ▼
Streamlit app (app.py) ── Leaflet custom component (map_component/)
```

- `etl/scrape_myciti.py` downloads every route's timetable PDF and parses the
  tables (one row per stop; day type and direction read from page headers).
- `etl/load_db.py` loads routes, stops and ~170k departure times into DuckDB.
- `system_map.py` builds the map network: stop order along each route is
  reconstructed from the timetable itself (the first trip of the day visits
  stops in sequence), then stops are matched by name to the city's official
  stops layer for coordinates.
- `map_component/index.html` is a bidirectional Streamlit custom component —
  clicking a stop on the map sends its name back to Python.

## Database schema

```mermaid
erDiagram
    routes ||--o{ stops : "route_id"
    routes ||--o{ departures : "route_id"
    routes ||--o{ timetables : "route_id"
    stops }o..o{ departures : "joined by stop_name"

    routes {
        varchar route_id PK "e.g. T01, D04, 101"
        varchar route_name
        varchar route_description
        varchar detail_url "timetable PDF"
        timestamp scraped_at
    }
    stops {
        varchar stop_id PK "route_id + ordinal"
        varchar stop_name
        varchar route_id FK
        int stop_sequence "order along route"
        varchar direction "e.g. 'To 101 Vredehoek'"
        double stop_lat "unused; coords live in cct_stops.geojson"
        double stop_lon
        timestamp scraped_at
    }
    departures {
        int id PK
        varchar route_id
        varchar stop_name
        varchar direction
        varchar day_type "weekday | saturday | sunday"
        varchar departure_time "HH:MM:SS"
        timestamp scraped_at
    }
    timetables {
        int id PK
        varchar route_id
        varchar route_name
        varchar day_type
        varchar timetable_url
        timestamp scraped_at
    }
    scrape_log {
        int run_id PK "auto via sequence"
        timestamp started_at
        timestamp finished_at
        int routes_loaded
        int stops_loaded
        int departures_loaded
        varchar status "success | error"
        varchar notes
    }
    stop_blocks {
        varchar stop_name PK
        int block "CCT load shedding block 1..16, NULL if unmapped"
    }
```

`departures` is the core fact table (~170k rows) the app queries; `stops` ↔
`departures` join on `stop_name` rather than a foreign key because the PDFs
identify stops only by name. `scrape_log` is a standalone audit table, one
row per ETL run.

## Getting started

Requires Python 3.10+.

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Run the app
streamlit run app.py
```

That's it — the repo ships with a Parquet snapshot of the timetable data
(`data/snapshot/`), and the app builds its DuckDB database from it
automatically on first start. This is also what makes one-click deploys
(e.g. Streamlit Community Cloud) work.

To refresh the data from myciti.org.za (takes a few minutes):

```bash
python3 run_etl.py
```

The ETL rebuilds the database **and** re-exports the snapshot — commit the
updated `data/snapshot/*.parquet` files so deployments pick up the new
timetables. `run_etl.py --inspect` prints what's in the database without
re-scraping.

## Project structure

```
├── app.py                    # Streamlit app (search, timetables, map tabs)
├── system_map.py             # System-map graph builder + component wrapper
├── map_component/
│   └── index.html            # Leaflet map frontend (custom component)
├── etl/
│   ├── scrape_myciti.py      # PDF scraper/parser
│   ├── load_db.py            # DuckDB loader
│   └── build_stop_blocks.py  # Optional stop-to-loadshedding-block mapping
├── run_etl.py                # One-command ETL pipeline
├── data/
│   ├── cct_stops.geojson     # Stop coordinates (City of Cape Town open data)
│   ├── cct_routes.geojson    # Street route geometries (City of Cape Town)
│   ├── snapshot/             # Parquet snapshot — app rebuilds the DB from it
│   └── myciti.duckdb         # Built from snapshot or ETL (not committed)
└── requirements.txt
```

## Load shedding awareness

The sidebar shows the load shedding stage the app is working with. The
effective stage is resolved in this order:

1. **Manual override**: pick a stage (0 to 8) in the sidebar selectbox.
   Useful for "what if" checks and for when the API is unavailable.
2. **Live stage**: with the selectbox on Auto and an API key configured,
   the app reads the national Eskom stage from the
   [EskomSePush API](https://eskomsepush.gumroad.com/l/api) every 30
   minutes (48 calls/day, inside the free tier's 50/day allowance).
3. **Default**: without an override or API key, the app assumes stage 0.

The badge under the selectbox names the source in use: `manual override`,
`live · EskomSePush`, or `assumed · no API key`.

To enable the live source, create `.streamlit/secrets.toml`:

```toml
ESP_API_KEY = "your-eskomsepush-key"
```

or set the `ESP_API_KEY` environment variable. API failures never break
the app: the stage silently falls back to the next source in the list.

### Mapping stops to load shedding blocks

Load shedding in Cape Town rotates through 16 city-defined area blocks.
To know which block each bus stop sits in, the ETL can join stop
coordinates against the city's block polygons:

1. Download the **Load shedding areas** dataset as GeoJSON from the
   [City of Cape Town Open Data Portal](https://odp-cctegis.opendata.arcgis.com/).
2. Save it as `data/cct_loadshedding_areas.geojson`.
3. Rerun the mapping step:

   ```bash
   python3 etl/build_stop_blocks.py   # or the full pipeline: python3 run_etl.py
   ```

This produces a `stop_blocks` table (and
`data/snapshot/stop_blocks.parquet`). Stops that fall outside every
polygon keep a NULL block. The step is optional: without the polygon
file, `run_etl.py` skips it and the app simply carries no block data.
No block assignments are ever guessed.

## Data sources & credits

- Timetables: [MyCiTi](https://www.myciti.org.za) route timetable PDFs
- Stop coordinates & route geometries:
  [City of Cape Town Open Data Portal](https://odp-cctegis.opendata.arcgis.com/)
- Basemap tiles: [CARTO](https://carto.com/attributions) /
  [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors

This is an unofficial hobby project, not affiliated with MyCiTi or the City of
Cape Town. Timetable data is only as fresh as the last ETL run — always check
official sources before travelling.

## Known limitations

- Timetable-based only — no real-time vehicle tracking
- Public holidays follow the Sunday timetable but are not auto-detected
- A few of the newest routes/stops are missing from the city's open-data
  layers: 4 routes fall back to straight dashed lines in street mode, and
  ~23 stops are not shown on the map (they still appear in search)

## License

[MIT](LICENSE)
