# Deployment Guide — MyCiTi Bus Timetable

Production surface is **FastAPI + Jinja** (`fastapi_app/`). Streamlit (`app.py`) is for local development only.

---

## Cold start / DuckDB boot pin (required reading)

Cold start must **not** run a full PDF ETL. The process rebuilds DuckDB from committed Parquet snapshots.

| Item | Value |
|------|--------|
| Start command | `uvicorn fastapi_app.main:app --host 0.0.0.0 --port $PORT` |
| Snapshot path | `data/snapshot/*.parquet` (routes, stops, departures, timetables, scrape_log, …) |
| Runtime DB | `data/myciti.duckdb` — **rebuilt on boot** by `fastapi_app/dependencies.py` → `_ensure_database` when missing |
| Fail-loud | If the `.duckdb` file and the snapshot directory are both missing, startup raises `FileNotFoundError` — do **not** serve empty boards |
| Health | `GET /health` — fast JSON (`status`, `version`, `db_ready`, `scraped_at`); non-200 / `degraded` when DB/snapshot missing; never blocks on ETL |

`scraped_at` for the trust strip and `/health` comes from `scrape_log.finished_at` (latest successful ETL). If absent, the UI shows an honest “unknown — run ETL to refresh” (never a fake date).

---

## Option 1: Railway (preferred host notes)

**Product note (Oct 2026):** Railway deploy is currently **parked** by product — keep these docs accurate for the day it is unpaused. Do not treat a live Railway URL as required for Now 2–4 merge.

**Why Railway:** fast deploys, simple GitHub connect, `$PORT` injection.

### Steps (when unpaused)

1. Sign up: https://railway.app → Login with GitHub  
2. New Project → Deploy from GitHub repo → `mycity-bus-schedule`  
3. Start command (if not auto-detected):  
   `uvicorn fastapi_app.main:app --host 0.0.0.0 --port $PORT`  
4. Ensure `data/snapshot/*.parquet` is in the deployed tree (committed in repo).  
5. Optional env: `ESP_API_KEY` for live load-shedding stage.  
6. Settings → Generate Domain. Probe `GET /health` until `status=ok` and `db_ready=true`.

---

## Option 2: Render (alternative)

`render.yaml` is present for a free-tier Web Service. Same start command and snapshot rules as above.

1. https://render.com → New → Web Service → connect the GitHub repo  
2. Confirm start: `uvicorn fastapi_app.main:app --host 0.0.0.0 --port $PORT`  
3. Free tier spins down after idle — first request is a cold start; `/health` and the Jinja shell (honest wake banner) should respond without a blank page once the process is up  
4. Optional: `ESP_API_KEY`

---

## Option 3: Fly.io (edge)

```bash
fly auth login
fly launch   # accept defaults; region near Cape Town / Johannesburg
fly deploy
```

Use the same uvicorn start command and snapshot path.

---

## Pre-deploy checklist

```bash
# Local smoke (uses snapshot → DuckDB boot path)
uvicorn fastapi_app.main:app --port 8000 --reload

curl -s localhost:8000/health
# expect: {"status":"ok","version":"2.0.0","db_ready":true,"scraped_at":"..."}

# Open search chrome (From / To above the fold) + trust strip after a search
open http://localhost:8000
```

Also see `qa/live-smoke-checklist.md` once a public host is live.

---

## After deploy

1. `GET /health` → `status=ok`, `db_ready=true`  
2. Homepage shows search chrome immediately (not blank white)  
3. Journey results and stop boards show Compliance trust lines 1–3; footer shows sources (line 5)  
4. Sample journey (e.g. Camps Bay → Civic Centre) returns rows or an explicit empty state  

---

## Troubleshooting

**App won't start / loud error about snapshot**  
→ Ensure `data/snapshot/*.parquet` is deployed. Do not expect PDF scraping on cold start.

**`/health` returns 503 / `degraded`**  
→ Connection pool or `scrape_log` missing. Check logs for `_ensure_database` / DuckDB errors.

**Slow first load on free tier**  
→ Process spin-up, then snapshot→DuckDB rebuild. Subsequent requests use the pool. Honest wake copy appears when `db_ready` is false.

**Static files missing**  
→ `fastapi_app/main.py` mounts `/static` from `fastapi_app/static/`.

---

## Updating timetable data

Run ETL locally (or in CI), refresh Parquet snapshots, commit:

```bash
python run_etl.py
# confirm data/snapshot/*.parquet updated, then commit
```

Hosts rebuild `.duckdb` from those snapshots on next cold start.

---

**Questions?** FastAPI deployment: https://fastapi.tiangolo.com/deployment/
