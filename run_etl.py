"""
run_etl.py — Multi-Operator ETL Runner
========================================
One-command pipeline for scraping and loading timetable data from
MyCiTi and/or UCT shuttle into data/myciti.duckdb.

Usage:
    python3 run_etl.py                # scrape + load MyCiTi only (default)
    python3 run_etl.py --uct          # scrape + load UCT only
    python3 run_etl.py --all          # scrape + load both operators
    python3 run_etl.py --uct --offline  # UCT from cached PDFs
    python3 run_etl.py --inspect      # show what's in the DB
"""

import argparse
import logging
import sys
from pathlib import Path

# Make the project root importable before the etl imports
sys.path.insert(0, str(Path(__file__).parent))

# pylint: disable=wrong-import-position
from etl.load_db import load, inspect, DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def _run_myciti(db_path: Path) -> None:
    """Scrape and load MyCiTi timetables."""
    from etl.scrape_myciti import scrape_all as scrape_myciti  # pylint: disable=import-outside-toplevel

    log.info("=== MyCiTi ETL ===")
    data = scrape_myciti()

    total_records = sum(len(v) for v in data.values())
    if total_records == 0:
        log.error(
            "MyCiTi scrape returned no data.\n"
            "  • Check your internet connection.\n"
            "  • Make sure myciti.org.za is accessible from your machine.\n"
            "  • The website structure may have changed — update selectors in "
            "etl/scrape_myciti.py."
        )
        return

    load(data, db_path, operator="myciti")


def _run_uct(db_path: Path, offline: bool = False) -> None:
    """Scrape and load UCT shuttle timetables."""
    from etl.scrape_uct import scrape_all as scrape_uct  # pylint: disable=import-outside-toplevel

    log.info("=== UCT Shuttle ETL ===")
    data = scrape_uct(offline=offline)

    # The 'inconsistencies' key is informational — not loaded into the DB
    inconsistencies = data.pop("inconsistencies", [])

    total_records = sum(len(v) for v in data.values())
    if total_records == 0:
        log.error(
            "UCT scrape returned no data.\n"
            "  • Check your internet connection, or use --offline.\n"
            "  • If uct.ac.za is blocked, save HTML/PDFs to data/uct_raw/ first."
        )
        return

    load(data, db_path, operator="uct")

    if inconsistencies:
        log.info(f"UCT data inconsistencies logged: {len(inconsistencies)}")


def main() -> None:
    """Parse args and run the ETL pipeline."""
    parser = argparse.ArgumentParser(
        description="Multi-operator ETL — scrape & load into DuckDB"
    )
    parser.add_argument(
        "--inspect", action="store_true",
        help="Skip scraping; just inspect the current DB contents.",
    )
    parser.add_argument(
        "--db", type=str, default=str(DB_PATH),
        help=f"Path to DuckDB file (default: {DB_PATH})",
    )
    parser.add_argument(
        "--uct", action="store_true",
        help="Run UCT shuttle ETL (instead of MyCiTi).",
    )
    parser.add_argument(
        "--all", action="store_true", dest="run_all",
        help="Run ETL for all operators (MyCiTi + UCT).",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="UCT: parse from cached PDFs in data/uct_raw/ instead of fetching.",
    )
    args = parser.parse_args()

    db_path = Path(args.db)

    if args.inspect:
        if not db_path.exists():
            log.error(f"Database not found at {db_path}. Run without --inspect first.")
            sys.exit(1)
        inspect(db_path)
        return

    log.info("Starting ETL pipeline …")
    log.info(f"Target database: {db_path}")

    # Determine which operators to run
    if args.run_all:
        _run_myciti(db_path)
        _run_uct(db_path, offline=args.offline)
    elif args.uct:
        _run_uct(db_path, offline=args.offline)
    else:
        # Default: MyCiTi only (backwards compatible)
        _run_myciti(db_path)

    # Optional: map stops to load shedding blocks — needs the CCT
    # polygon layer, which is downloaded separately; skip cleanly otherwise.
    from etl.build_stop_blocks import build, AREAS_PATH  # pylint: disable=import-outside-toplevel
    if AREAS_PATH.exists():
        build(db_path)
    else:
        log.info(
            "Stop blocks skipped: %s not found; see etl/build_stop_blocks.py "
            "for download instructions.", AREAS_PATH.name,
        )

    # Print summary
    inspect(db_path)

    log.info("ETL pipeline finished.")


if __name__ == "__main__":
    main()
