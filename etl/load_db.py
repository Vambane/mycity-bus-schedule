"""
etl/load_db.py — DuckDB Loader
================================
Takes the structured data returned by scrape_myciti.scrape_all()
and loads (or refreshes) it into a DuckDB database at data/myciti.duckdb.

Schema
------
  routes       — one row per route
  stops        — one row per stop per route (with direction & sequence)
  timetables   — metadata about each timetable page scraped
  departures   — individual departure times (the core query table)
  scrape_log   — audit log of each ETL run

Usage
-----
    python3 etl/load_db.py
"""

import logging
from datetime import datetime
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
    scraped_at    TIMESTAMP
);

-- Timetable pages metadata
CREATE TABLE IF NOT EXISTS timetables (
    id            INTEGER PRIMARY KEY,
    route_id      VARCHAR NOT NULL,
    route_name    VARCHAR,
    day_type      VARCHAR,   -- 'weekday' | 'saturday' | 'sunday'
    timetable_url VARCHAR,
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
    status        VARCHAR,
    notes         VARCHAR
);
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Clear all data tables before a fresh load (full-refresh strategy)."""
    for table in ["departures", "timetables", "stops", "routes"]:
        con.execute(f"DELETE FROM {table}")
    log.info("All tables truncated — ready for fresh load.")


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

def load(data: dict[str, list[dict]], db_path: Path = DB_PATH) -> None:
    """
    Load scraped MyCiTi data into DuckDB.

    Args:
        data:    Dict returned by scrape_myciti.scrape_all().
        db_path: Path to the DuckDB file (created if it doesn't exist).
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    log.info(f"Opening DuckDB at {db_path}")

    con = duckdb.connect(str(db_path))
    started_at = datetime.utcnow()

    try:
        # Ensure schema exists
        con.execute(DDL)
        log.info("Schema verified / created.")

        # Full-refresh: truncate and reload
        _truncate_tables(con)

        # --- routes ---
        routes_loaded = _insert_rows(
            con, "routes", data["routes"],
            ["route_id", "route_name", "route_description", "detail_url", "scraped_at"],
        )

        # --- stops ---
        stops_loaded = _insert_rows(
            con, "stops", data["stops"],
            ["stop_id", "stop_name", "route_id", "stop_sequence",
             "direction", "stop_lat", "stop_lon", "scraped_at"],
        )

        # --- timetables ---
        # Assign surrogate IDs before inserting
        for i, row in enumerate(data["timetables"], start=1):
            row.setdefault("id", i)
        _insert_rows(
            con, "timetables", data["timetables"],
            ["id", "route_id", "route_name", "day_type", "timetable_url", "scraped_at"],
        )

        # --- departures ---
        for i, row in enumerate(data["departures"], start=1):
            row.setdefault("id", i)
        departures_loaded = _insert_rows(
            con, "departures", data["departures"],
            ["id", "route_id", "stop_name", "direction",
             "day_type", "departure_time", "scraped_at"],
        )

        # --- audit log ---
        finished_at = datetime.utcnow()
        # run_id is computed explicitly (not via the sequence default): a DB
        # rebuilt from the Parquet snapshot has the table without the default.
        con.execute(
            """
            INSERT INTO scrape_log
              (run_id, started_at, finished_at, routes_loaded, stops_loaded,
               departures_loaded, status, notes)
            VALUES ((SELECT COALESCE(MAX(run_id), 0) + 1 FROM scrape_log),
                    ?, ?, ?, ?, ?, 'success', ?)
            """,
            [
                started_at.isoformat(),
                finished_at.isoformat(),
                routes_loaded,
                stops_loaded,
                departures_loaded,
                f"Full refresh completed in "
                f"{(finished_at - started_at).total_seconds():.1f}s",
            ],
        )

        log.info(
            f"Load complete — "
            f"{routes_loaded} routes, {stops_loaded} stops, "
            f"{departures_loaded} departures."
        )

        # Export a Parquet snapshot alongside the DB. The .duckdb file is
        # gitignored; committing the snapshot lets a fresh deployment
        # (e.g. Streamlit Cloud) rebuild the database without scraping.
        snapshot_dir = db_path.parent / "snapshot"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        for table in ["routes", "stops", "timetables", "departures", "scrape_log"]:
            con.execute(
                f"COPY {table} TO '{snapshot_dir / table}.parquet' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        log.info(f"Snapshot exported to {snapshot_dir}/")

    except Exception as exc:
        log.error(f"Load failed: {exc}")
        con.execute(
            """
            INSERT INTO scrape_log (run_id, started_at, finished_at, status, notes)
            VALUES ((SELECT COALESCE(MAX(run_id), 0) + 1 FROM scrape_log),
                    ?, ?, 'error', ?)
            """,
            [started_at.isoformat(), datetime.utcnow().isoformat(), str(exc)],
        )
        raise

    finally:
        con.close()


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

    print("\n--- Sample routes ---")
    try:
        rows = con.execute("SELECT route_id, route_name FROM routes LIMIT 10").fetchall()
        for r in rows:
            print(f"  {r[0]:<10} {r[1]}")
    except Exception as e:
        print(f"  (error: {e})")

    print("\n--- Sample departures ---")
    try:
        rows = con.execute(
            """
            SELECT route_id, stop_name, day_type, departure_time
            FROM   departures
            ORDER  BY route_id, day_type, departure_time
            LIMIT  10
            """
        ).fetchall()
        for r in rows:
            print(f"  {r[0]:<8} {r[2]:<10} {r[3]}  {r[1]}")
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
