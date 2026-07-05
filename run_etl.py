"""
run_etl.py — MyCiTi ETL Runner
================================
One-command pipeline:
    1. Scrapes routes, stops and departure times from myciti.org.za
    2. Loads everything into data/myciti.duckdb

Usage:
    python3 run_etl.py           # full scrape + load
    python3 run_etl.py --inspect # just show what's in the DB
"""

import argparse
import logging
import sys
from pathlib import Path

# Make the project root importable
sys.path.insert(0, str(Path(__file__).parent))

from etl.scrape_myciti import scrape_all
from etl.load_db import load, inspect, DB_PATH

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def main() -> None:
    """Parse args and run the ETL pipeline."""
    parser = argparse.ArgumentParser(description="MyCiTi ETL — scrape & load into DuckDB")
    parser.add_argument(
        "--inspect", action="store_true",
        help="Skip scraping; just inspect the current DB contents."
    )
    parser.add_argument(
        "--db", type=str, default=str(DB_PATH),
        help=f"Path to DuckDB file (default: {DB_PATH})"
    )
    args = parser.parse_args()

    db_path = Path(args.db)

    if args.inspect:
        if not db_path.exists():
            log.error(f"Database not found at {db_path}. Run without --inspect first.")
            sys.exit(1)
        inspect(db_path)
        return

    log.info("Starting MyCiTi ETL pipeline …")
    log.info(f"Target database: {db_path}")

    # Step 1: Scrape
    data = scrape_all()

    total_records = sum(len(v) for v in data.values())
    if total_records == 0:
        log.error(
            "Scrape returned no data.\n"
            "  • Check your internet connection.\n"
            "  • Make sure myciti.org.za is accessible from your machine.\n"
            "  • The website structure may have changed — update selectors in etl/scrape_myciti.py."
        )
        sys.exit(1)

    # Step 2: Load into DuckDB
    load(data, db_path)

    # Step 3: Print summary
    inspect(db_path)

    log.info("ETL pipeline finished. You can now run: streamlit run app.py")


if __name__ == "__main__":
    main()
