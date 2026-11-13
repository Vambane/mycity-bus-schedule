# Cloudflare Pages preview for Halte

The Render service in `render.yaml` stays the production app. This build is a second, static copy: journey search, stop boards, journey pages and the network map run in the browser from the Parquet snapshot committed in `data/snapshot/`. A preview URL does not wake Render, and nothing here changes DNS.

Do not point the live domain at Pages until you have clicked through a real `pages.dev` preview. Rollback is deleting that custom domain. Render keeps serving the Python app, including `ESP_API_KEY` if you set it there.

## What the static site does

| Page | How |
|---|---|
| `/` | The same landing page. A query string is sent to `/search` (Pages Function, or a small script if you upload `dist/` only). |
| `/search` | Direct trips, one transfer, then two transfers. Same rules as `journey.py`: 3 minute minimum change, 45 minute maximum wait, Pareto cut. |
| `/stop/<name>` | Departure board, including the after-midnight carry of stored `24:xx` times and "tomorrow's first buses" after the last departure. |
| `/journey` | Stop list for a direct trip or a transfer token. Same query shape as the Python app. |
| `/map` | Precomputed network graph. Trip preview interpolates a ghost marker in the browser. Caption stays "Estimated from timetable — not live GPS." |
| `/health` | JSON: `status`, `version`, `db_ready`, `mode: static`, `scraped_at`. |

The clock for "leave now", live boards, UCT expiry and public holidays is **Africa/Johannesburg** (UTC+2, no daylight saving). The page does not use the phone's local timezone for those decisions. `<input type="date">` still shows the browser's own date format; the hint says the Weekday / Saturday / Sunday control picks the timetable.

Supported modes stay MyCiTi, UCT Jammie and Metrorail, with the All / MyCiTi / UCT / Metrorail toggle. UCT routes past `valid_until` are hidden and named. Sunday and stale Saturday Metrorail notes match `metrorail_notes.py`.

## Build

From the repo root, with the same Python dependencies as the app (`requirements.txt`):

```bash
python pages/build_site.py --out dist
python pages/dev_server.py --port 4173
```

Open http://127.0.0.1:4173. The dev server applies the same rewrites as `dist/_redirects`.

The build reads `data/snapshot/*.parquet` in memory. It does not scrape, does not write `data/myciti.duckdb`, and does not call EskomSePush. Output is deterministic for a given snapshot and `pages/` source tree.

`dist/` is gitignored. Commit the Parquet snapshot, not the generated site.

### Refresh the timetable

```bash
python3 run_etl.py --all          # or one operator; see README
python pages/build_site.py --out dist
```

Commit the updated `data/snapshot/*.parquet`, then redeploy the Pages project. The Render service picks up the same snapshot on its next cold start. One operator's ETL does not rewrite the others.

The trust line shows the latest successful `scrape_log` time, labelled Africa/Johannesburg, plus a per-operator line so a newer Metrorail import is not presented as a MyCiTi refresh.

## Free-tier preview (do this before any domain change)

1. Leave the Render service, its env vars, and the custom domain exactly as they are.
2. In Cloudflare, create a **Pages** project (Workers & Pages → Create → Pages → Connect to Git). Use the free plan. Do not add a custom domain yet.
3. Build settings:
   - Production branch: this branch, until you choose to merge. `main` does not contain the static build until then, so a Pages project pointed at `main` too early will fail the build.
   - Build command: `pip install -r requirements.txt && python pages/build_site.py --out dist`
   - Build output directory: `dist`
   - Root directory: repository root (so `/functions` is picked up).
   - Environment variables: none. Do not add `ESP_API_KEY` to Pages.
4. Deploy. Cloudflare prints a preview URL such as `https://<branch>.<project>.pages.dev`.
5. Check, on a phone-sized window and on a desktop window:
   - `/health` is JSON with `"mode": "static"` and `"db_ready": true`.
   - A weekday Camps Bay → Civic Centre search returns timed cards and a journey page.
   - `/stop/Civic%20Centre` lists departures.
   - `/map` draws the network. A "Preview this trip" link shows the ghost caption, not a live position.
   - Sunday + Metrorail says there are no Sunday trains.
   - The badge says load shedding is unavailable, and the search still returns times.
6. Only after that, add the custom domain in the Pages project **and** remove it from Render in the same change window. If the preview is wrong, delete the Pages custom domain. Render is still the live app.

Pages Functions (`functions/index.js`) answer `GET /?from_stop=…` with a 301 to `/search?…`. Direct upload of `dist/` alone does not include that function; the landing page then replaces the URL in the browser.

## Routing, headers, limits

`dist/_redirects` keeps these URLs working without a Python process:

```
/health     /health.json         200
/search     /search/index.html   200
/journey    /journey/index.html  200
/map        /map/index.html      200
/stop/*     /stop/index.html     200
```

`dist/_headers` sets `nosniff`, a referrer policy, `X-Frame-Options: SAMEORIGIN` (the map iframe is same-origin), HSTS, and a Content-Security-Policy. Scripts and styles may load Leaflet from `unpkg.com`. Map tiles may load from `tile.openstreetmap.org`. `connect-src` also allows `https://*.workers.dev` for the optional Worker below. A Worker on any other host will be blocked until you add that host to `connect-src`.

Geolocation is allowed for the page itself (`Permissions-Policy: geolocation=(self)`). The nearest-stop button keeps the position in the browser. It is not sent to a server.

Cloudflare Pages free limits used as a budget: 25 MiB per file, 20,000 files. The build refuses to finish if it exceeds either. A current snapshot is about 34 files and under 3 MB. The weekday timetable is about 900 KB raw and about 120 KB gzipped, and it is fetched only when a search needs that day. The map graph (about 400 KB) loads only on `/map`. The landing page does not download a timetable.

HTML is `must-revalidate`. Hashed files under `/data/v/<snapshot>/` are immutable. `/data/ls.json` is `no-cache` so a Worker URL can be turned on without renaming the timetable files.

## Load shedding

Core timetables do not call Render or EskomSePush.

The badge says **unavailable** until you opt in. Per-stop outage flags stay off: the committed snapshot has no load-shedding block column, and the City polygon file is not in the repo. That matches a Render boot that never ran `etl/build_stop_blocks.py`.

To show the national stage without putting the key in the browser:

1. Copy `pages/worker/wrangler.toml.example` to `wrangler.toml` next to `pages/worker/load-shedding.js`.
2. `npx wrangler secret put ESP_API_KEY` (the secret stays in the Worker, not in git or in Pages env).
3. `npx wrangler deploy` from that directory. Free Workers quota applies. The Worker caches the stage for 30 minutes **per Cloudflare location**, so a busy global cache can still use more than EskomSePush's free 50 calls a day. Watch the quota.
4. Set `"endpoint"` in `data/ls.json` inside the built site to the Worker URL (`https://<name>.<account>.workers.dev`). Redeploy Pages. Do not put the key in that file.
5. If the Worker host is not `*.workers.dev`, add it to `connect-src` in `pages/build_site.py` (`HEADERS`) and rebuild.

The Worker returns `{available, stage, source: "auto"}` or `{available: false}`. It never echoes the key. If the call fails, the page stays on "unavailable" and the timetable still works.

The Streamlit app's manual stage picker and departure timeline chart are not part of this static site. They remain in `app.py` for local use.

## Tests

```bash
pytest tests/test_pages_parity.py tests/test_pages_build.py tests/test_pages_ui.py
```

Parity runs the Python search, transfer and stop-board code against the Node planner on representative cases: MyCiTi direct trips on all three day types, operator filters, arrive-by, UCT one-transfer, a MyCiTi/UCT two-transfer, a pair with no service, Metrorail direct and Sunday, a public holiday, late evening "next timetable", and just after midnight. Calendar dates from 2024 through 2030, including Easter, are compared with `service_day.py`.

Two itineraries with the same departure, arrival and transfer count can differ by which route the Pareto step keeps. DuckDB does not order that tie; the static planner breaks it by route id. Both answers satisfy `journey.py`. The test checks that the static itinerary is one of the Python candidates and that the departure, arrival and transfer count match.

CI installs Node 22 and runs these tests with the rest of the suite. The browser test skips when Chromium is not installed. On the Python 3.12 job, Playwright already installs Chromium.

## Known gaps

- Load-shedding **flags on individual journeys** need block polygons that are not in the snapshot. The stage badge can come from the Worker; the flags cannot, until `stop_blocks` is exported.
- UCT stops still have no coordinates, same as the Python app, so "Use my location" cannot pick a Jammie stop.
- A few MyCiTi stops are still missing from the city layer and draw as gaps or straight segments.
- The static order of two buses at the same minute is by route id. The Render page follows DuckDB's row order for that tie.
- This is not an offline app. The first search downloads that day's timetable from Pages.
