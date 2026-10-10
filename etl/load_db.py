"""
etl/load_db.py — DuckDB Loader
================================
Takes the structured data returned by scrape_myciti.scrape_all() (or
scrape_uct.scrape_all()) and loads it into a DuckDB database at
data/myciti.duckdb.

Each operator's data is loaded independently: a full-refresh for one
operator leaves the other operator's rows untouched.

Schema
------
  routes       — one row per route (keyed by operator + route_id)
  stops        — one row per stop per route (with direction & sequence)
  timetables   — metadata about each timetable page scraped
  departures   — individual departure times (the core query table)
  scrape_log   — audit log of each ETL run

Usage
-----
    python3 etl/load_db.py
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

import duckdb

log = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "data" / "myciti.duckdb"

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

DDL = """
-- Routes dimension
CREATE TABLE IF NOT EXISTS routes (
    route_id          VARCHAR PRIMARY KEY,
    route_name        VARCHAR,
    route_description VARCHAR,
    detail_url        VARCHAR,
    operator          VARCHAR DEFAULT 'myciti',  -- 'myciti' | 'uct'
    scraped_at        TIMESTAMP
);

-- Stops dimension: one row per stop per route direction
CREATE TABLE IF NOT EXISTS stops (
    stop_id       VARCHAR PRIMARY KEY,
    stop_name     VARCHAR NOT NULL,
    route_id      VARCHAR NOT NULL REFERENCES routes(route_id),
    stop_sequence INTEGER,
    direction     VARCHAR,   -- 'outbound' | 'inbound'
    stop_lat      DOUBLE,
    stop_lon      DOUBLE,
    operator      VARCHAR DEFAULT 'myciti',  -- 'myciti' | 'uct'
    scraped_at    TIMESTAMP
);

-- Timetable pages metadata
CREATE TABLE IF NOT EXISTS timetables (
    id            INTEGER PRIMARY KEY,
    route_id      VARCHAR NOT NULL,
    route_name    VARCHAR,
    day_type      VARCHAR,   -- 'weekday' | 'saturday' | 'sunday'
    timetable_url VARCHAR,
    valid_from    DATE,      -- timetable validity start (NULL for MyCiTi)
    valid_until   DATE,      -- timetable validity end (NULL for MyCiTi)
    operator      VARCHAR DEFAULT 'myciti',  -- 'myciti' | 'uct'
    scraped_at    TIMESTAMP
);

-- Core fact table: individual departure times
CREATE TABLE IF NOT EXISTS departures (
    id             INTEGER PRIMARY KEY,
    route_id       VARCHAR NOT NULL,
    stop_name      VARCHAR NOT NULL,
    direction      VARCHAR,
    day_type       VARCHAR NOT NULL,  -- 'weekday' | 'saturday' | 'sunday'
    departure_time VARCHAR NOT NULL,  -- HH:MM:SS
    operator       VARCHAR DEFAULT 'myciti',  -- 'myciti' | 'uct'
    scraped_at     TIMESTAMP
);

-- Sequence for scrape_log primary key (DuckDB does not auto-increment INTEGER PK)
CREATE SEQUENCE IF NOT EXISTS seq_scrape_log_run_id START 1;

-- ETL audit log
CREATE TABLE IF NOT EXISTS scrape_log (
    run_id        INTEGER PRIMARY KEY DEFAULT nextval('seq_scrape_log_run_id'),
    started_at    TIMESTAMP,
    finished_at   TIMESTAMP,
    routes_loaded INTEGER,
    stops_loaded  INTEGER,
    departures_loaded INTEGER,
    operator      VARCHAR DEFAULT 'myciti',  -- 'myciti' | 'uct'
    status        VARCHAR,
    notes         VARCHAR
);
"""

# ---------------------------------------------------------------------------
# Migration: add new columns to existing databases that lack them.
# Each statement is idempotent — safe to run on every startup.
# ---------------------------------------------------------------------------

MIGRATIONS = [
    "ALTER TABLE routes     ADD COLUMN IF NOT EXISTS operator VARCHAR DEFAULT 'myciti'",
    "ALTER TABLE stops      ADD COLUMN IF NOT EXISTS operator VARCHAR DEFAULT 'myciti'",
    "ALTER TABLE timetables ADD COLUMN IF NOT EXISTS operator VARCHAR DEFAULT 'myciti'",
    "ALTER TABLE departures ADD COLUMN IF NOT EXISTS operator VARCHAR DEFAULT 'myciti'",
    "ALTER TABLE timetables ADD COLUMN IF NOT EXISTS valid_from DATE",
    "ALTER TABLE timetables ADD COLUMN IF NOT EXISTS valid_until DATE",
    "ALTER TABLE scrape_log ADD COLUMN IF NOT EXISTS operator VARCHAR DEFAULT 'myciti'",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate_tables(
    con: duckdb.DuckDBPyConnection,
    operator: str = "myciti",
) -> None:
    """Delete rows for one operator before a fresh load.

    Only the specified operator's data is removed — other operators'
    rows survive intact. This lets MyCiTi and UCT ETL runs happen
    independently without interfering with each other.
    """
    for table in ["departures", "timetables", "stops", "routes"]:
        con.execute(f"DELETE FROM {table} WHERE operator = ?", [operator])
    log.info(f"Tables truncated for operator={operator} — ready for fresh load.")


def _insert_rows(
    con: duckdb.DuckDBPyConnection,
    table: str,
    rows: list[dict],
    columns: list[str],
) -> int:
    """
    Bulk-insert rows into a table.

    Args:
        con:     Open DuckDB connection.
        table:   Target table name.
        rows:    List of dicts (extra keys are ignored, missing keys → None).
        columns: Ordered list of column names to insert.

    Returns:
        Number of rows inserted.
    """
    if not rows:
        return 0

    placeholders = ", ".join(["?"] * len(columns))
    col_list = ", ".join(columns)
    sql = f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})"

    values = [
        tuple(row.get(col) for col in columns)
        for row in rows
    ]
    con.executemany(sql, values)
    log.info(f"  Inserted {len(values)} rows into {table}.")
    return len(values)


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------

def load(
    data: dict[str, list[dict]],
    db_path: Path = DB_PATH,
    operator: str = "myciti",
) -> None:
    """
    Load scraped data into DuckDB for a single operator.

    Only the specified operator's rows are replaced; the other
    operator's data remains intact.

    Args:
        data:     Dict with keys {routes, stops, timetables, departures}.
        db_path:  Path to the DuckDB file (created if it doesn't exist).
        operator: Operator tag — 'myciti' or 'uct'.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    log.info(f"Opening DuckDB at {db_path}")

    con = duckdb.connect(str(db_path))
    started_at = datetime.now(timezone.utc)

    try:
        # Ensure schema exists (fresh DB) …
        con.execute(DDL)
        # … and migrate existing DBs that lack new columns.
        for stmt in MIGRATIONS:
            con.execute(stmt)
        log.info("Schema verified / migrated.")

        # Stamp every row with its operator before inserting.
        for collection in data.values():
            for row in collection:
                row.setdefault("operator", operator)

        # Full-refresh for this operator only — other operators survive.
        _truncate_tables(con, operator=operator)

        # --- routes ---
        routes_loaded = _insert_rows(
            con, "routes", data["routes"],
            ["route_id", "route_name", "route_description",
             "detail_url", "operator", "scraped_at"],
        )

        # --- stops ---
        stops_loaded = _insert_rows(
            con, "stops", data["stops"],
            ["stop_id", "stop_name", "route_id", "stop_sequence",
             "direction", "stop_lat", "stop_lon", "operator", "scraped_at"],
        )

        # --- timetables ---
        # Assign surrogate IDs that don't collide across operators: offset
        # by MAX existing ID so a second operator's load is safe.
        max_id = con.execute(
            "SELECT COALESCE(MAX(id), 0) FROM timetables"
        ).fetchone()[0]
        for i, row in enumerate(data["timetables"], start=max_id + 1):
            row.setdefault("id", i)
        _insert_rows(
            con, "timetables", data["timetables"],
            ["id", "route_id", "route_name", "day_type", "timetable_url",
             "valid_from", "valid_until", "operator", "scraped_at"],
        )

        # --- departures ---
        max_dep_id = con.execute(
            "SELECT COALESCE(MAX(id), 0) FROM departures"
        ).fetchone()[0]
        for i, row in enumerate(data["departures"], start=max_dep_id + 1):
            row.setdefault("id", i)
        departures_loaded = _insert_rows(
            con, "departures", data["departures"],
            ["id", "route_id", "stop_name", "direction",
             "day_type", "departure_time", "operator", "scraped_at"],
        )

        # --- audit log ---
        finished_at = datetime.now(timezone.utc)
        con.execute(
            """
            INSERT INTO scrape_log
              (run_id, started_at, finished_at, routes_loaded, stops_loaded,
               departures_loaded, operator, status, notes)
            VALUES ((SELECT COALESCE(MAX(run_id), 0) + 1 FROM scrape_log),
                    ?, ?, ?, ?, ?, ?, 'success', ?)
            """,
            [
                started_at.isoformat(),
                finished_at.isoformat(),
                routes_loaded,
                stops_loaded,
                departures_loaded,
                operator,
                f"Full refresh ({operator}) completed in "
                f"{(finished_at - started_at).total_seconds():.1f}s",
            ],
        )

        log.info(
            f"Load complete ({operator}) — "
            f"{routes_loaded} routes, {stops_loaded} stops, "
            f"{departures_loaded} departures."
        )

        # Export a Parquet snapshot alongside the DB. The .duckdb file is
        # gitignored; committing the snapshot lets a fresh deployment
        # (e.g. Streamlit Cloud) rebuild the database without scraping.
        export_snapshot(con, db_path)

    except Exception as exc:
        log.error(f"Load failed: {exc}")
        try:
            con.execute(
                """
                INSERT INTO scrape_log
                  (run_id, started_at, finished_at, operator, status, notes)
                VALUES ((SELECT COALESCE(MAX(run_id), 0) + 1 FROM scrape_log),
                        ?, ?, ?, 'error', ?)
                """,
                [
                    started_at.isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    operator,
                    str(exc),
                ],
            )
        except Exception as log_exc:
            log.warning(f"  Additionally, scrape_log error insert failed: {log_exc}")
        raise

    finally:
        con.close()


def export_snapshot(
    con: duckdb.DuckDBPyConnection,
    db_path: Path = DB_PATH,
) -> None:
    """Export all tables to Parquet files in the snapshot directory."""
    snapshot_dir = db_path.parent / "snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for table in ["routes", "stops", "timetables", "departures", "scrape_log"]:
        con.execute(
            f"COPY {table} TO '{snapshot_dir / table}.parquet' "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
    log.info(f"Snapshot exported to {snapshot_dir}/")


# ---------------------------------------------------------------------------
# Quick DB inspection helper
# ---------------------------------------------------------------------------

def inspect(db_path: Path = DB_PATH) -> None:
    """Print row counts for every table — useful for sanity-checking the load."""
    con = duckdb.connect(str(db_path), read_only=True)
    tables = ["routes", "stops", "timetables", "departures", "scrape_log"]
    print("\n=== DuckDB contents ===")
    for t in tables:
        try:
            count = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"  {t:<20} {count:>6} rows")
        except Exception:
            print(f"  {t:<20} (not found)")

    # Show per-operator breakdown if the column exists
    print("\n--- Rows per operator ---")
    for t in ["routes", "departures"]:
        try:
            rows = con.execute(
                f"SELECT operator, COUNT(*) FROM {t} GROUP BY operator ORDER BY operator"
            ).fetchall()
            for op, cnt in rows:
                print(f"  {t:<20} {op or 'NULL':<10} {cnt:>6}")
        except Exception:
            pass

    print("\n--- Sample routes ---")
    try:
        rows = con.execute(
            "SELECT route_id, route_name, operator FROM routes LIMIT 10"
        ).fetchall()
        for r in rows:
            print(f"  {r[2] or 'myciti':<8} {r[0]:<10} {r[1]}")
    except Exception as e:
        print(f"  (error: {e})")

    print("\n--- Sample departures ---")
    try:
        rows = con.execute(
            """
            SELECT route_id, stop_name, day_type, departure_time, operator
            FROM   departures
            ORDER  BY operator, route_id, day_type, departure_time
            LIMIT  10
            """
        ).fetchall()
        for r in rows:
            print(f"  {r[4] or 'myciti':<8} {r[0]:<8} {r[2]:<10} {r[3]}  {r[1]}")
    except Exception as e:
        print(f"  (error: {e})")

    con.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from etl.scrape_myciti import scrape_all

    scraped = scrape_all()
    load(scraped)
    inspect()
