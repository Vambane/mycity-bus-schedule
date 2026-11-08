# Metrorail Integration

Technical details for the Metrorail Western Cape integration. For general
project information, see [README.md](README.md).

## Overview

Halte loads PRASA's Metrorail Western Cape timetables for all five lines
alongside MyCiTi and UCT, in the same DuckDB tables, tagged
`operator = 'metrorail'`.

| Route ID | Line | Weekday sheet | Saturday sheet |
|----------|------|---------------|----------------|
| MR-S | Southern Line | 2 Feb 2026 | 31 Oct 2025 |
| MR-CF | Cape Flats Line | 8 Jun 2026 | 23 Feb 2024 |
| MR-C | Central Line | 8 Jun 2026 | undated |
| MR-N | Northern Line | 8 Jun 2026 | 28 Dec 2024 |
| MR-M | Malmesbury Line | 8 Jun 2026 | 8 Jun 2026 |

About 7,900 departures on about 470 trains, 111 stations, all with
coordinates. The dates are stored as `timetables.valid_from`.

## Source

PRASA publishes timetables as image and PDF sheets
(<https://www.prasa.com/train-schedules/>). The open-source
[Next Train](https://nexttrain.co.za) project
([enock-elk/next-train-astro](https://github.com/enock-elk/next-train-astro))
transcribes them into a station by train-number grid and publishes
`public/data/full-database.json`. `etl/scrape_metrorail.py` reads that file.

> **Licence:** the Next Train repository has no licence file. Get the
> maintainer's permission before publishing the derived snapshot in a public
> repository or deploy, and credit Next Train (the footer and landing page
> already do).

```bash
python3 run_etl.py --metrorail            # fetch, cache to data/metrorail_raw/, load
python3 run_etl.py --metrorail --offline  # load from the cache
python3 run_etl.py --all                  # MyCiTi + UCT + Metrorail
```

Stop the app before running the ETL: DuckDB allows one writer, and a
running app holds the database file.

## How trains become Halte lines

Halte has no trips table. Direct search, transfers, departure boards, the
journey page, trip preview and the map all group departures by
`route_id + direction` and recover stop order from the earliest time at
each stop. That suits a bus route whose trips all call at the same stops.
A rail line mixes branches, short workings and express trains, so the
grouping broke: on real data 178 trains came out of order.

So each line is split into **stop patterns**: the trains that call at
exactly the same stations in the same order. Each pattern becomes one
`direction` with a rider-readable label:

| Pattern | Label |
|---------|-------|
| Main pattern for its end points | `Cape Town to Simon's Town` |
| Skips one or two stations | `Kraaifontein to Cape Town (skips Woltemade)` |
| Skips more | `Bellville to Cape Town (express)` |
| Different route between the same ends | `Cape Town to Strand via Monte Vista` |

Inside a pattern every trip visits every stop, so the k-th time at each
station is the k-th train and the existing engine pairs trips exactly. No
journey, board or map code had to learn about trips. The train number is
kept in the new `departures.trip_id` column (NULL for buses).

## Data handling

The loader logs each of these so a refresh shows what it did.

- **Trains across sheets.** A train printed on two sheets (Kraaifontein to
  Bellville, then Bellville to Cape Town) is merged. The sheet showing most
  of the train sets the station order; times that differ by a minute
  (arrival on one sheet, departure on the other) keep the fuller sheet's.
- **Reused numbers.** PRASA prints the same number for different trains on
  different sheets (3515 runs to Wellington via Mutual at 16:50 and to
  Bellville via Monte Vista at 18:10). A sighting that shares no station,
  or whose shared times differ by more than 2 minutes, is kept as a
  separate train `3515/2`.
- **Typos.** A time out of order with its neighbours (Saturday train 2504
  shows Stikland at 09:43 between Brackenfell 09:35 and Bellville 09:36) is
  dropped. The loader keeps the longest run of increasing times, so one
  typo costs one station.
- **Same-minute stations.** The later of two stations printed with the same
  minute gets one extra second. Pages show HH:MM; stop order becomes exact.
- **Names.** Stations are stored as `<Name> Station`. Eleven station names
  match a MyCiTi stop (Claremont, Mowbray, Strand, Woodstock, ...), and an
  exact name match would merge a bus stop and a station into one stop.
- **Coordinates.** Written to `data/metrorail_stations.csv` and matched by
  exact name only (`system_map._match_coords`), so no bus stop borrows a
  station's position through the fuzzy match.

## Day types

- **Weekday and Saturday** sheets are loaded.
- **Sunday:** Metrorail runs no Sunday service. A search or board involving
  a station says so instead of showing an empty result.
- **Public holidays:** not loaded. Halte runs every operator on its Sunday
  timetable on public holidays (`services/service_day.py`), so holiday
  trains stored as `sunday` would show on ordinary Sundays. Supporting
  them needs a holiday day type that falls back to Sunday for buses. The
  Sunday note tells riders to check PRASA on holidays.
- **Old Saturday sheets:** a Saturday search involving a station lists any
  sheet older than 180 days, or undated (`services/metrorail_notes.py`).

## App changes

- Network filter: All / MyCiTi / UCT / Metrorail. The value `both` still
  means every network, so old links keep working
  (`fastapi_app/config.py: OPERATOR_FILTERS`).
- Purple `rail` route badge; transfer chips read "Southern → Northern".
- Map: one fixed colour per line, a Metrorail legend group, and express
  patterns add no shortcut segments.
- "Use my location" includes stations.
- Direct search keeps rail rides from 1 minute (bus rides still need 5,
  which guards against bus trip mismatches; rail pairing is exact).
- Disclaimer, footer, trust strip and landing copy name PRASA and credit
  Next Train.

## Not done yet

- **Bus to train transfers.** Transfers happen where two lines share a stop
  name. A station and the bus stop outside it have different names
  ("Salt River Station", "Salt River Rail"), so mixed bus and train
  journeys are not found. This needs a table of interchange pairs with
  walking times, and the transfer search needs to use it.
- **Public holiday timetables** (see Day types).
- **Street geometry for rail.** Street view draws straight lines between
  stations; there is no rail layer like `cct_routes.geojson`.
- **December festive timetable,** once PRASA publishes it.
