"""Trust-strip helpers: scrape freshness and shared template context."""
from __future__ import annotations

import logging
import zoneinfo
from datetime import datetime
from typing import Any, Optional

import duckdb

from fastapi_app.config import settings
from fastapi_app.dependencies import is_pool_ready

logger = logging.getLogger(__name__)

UNKNOWN_SCRAPED_AT = "unknown — run ETL to refresh"

MYCITI_URL = "https://www.myciti.org.za"
UCT_TRANSPORT_URL = (
    "https://uct.ac.za/students/services-transport-parking-uct-shuttle/"
    "route-maps-timetables"
)

LOCATION_PRIVACY_LINE = (
    "Location stays on your device and is only used to suggest nearby stops."
)


def get_scraped_at_raw(conn: duckdb.DuckDBPyConnection) -> Optional[datetime]:
    """Latest successful ETL finish time from scrape_log, else None."""
    try:
        row = conn.execute(
            """
            SELECT MAX(finished_at)
            FROM scrape_log
            WHERE status = 'success'
            """
        ).fetchone()
        if not row or row[0] is None:
            return None
        value = row[0]
        if isinstance(value, datetime):
            return value
        return datetime.fromisoformat(str(value))
    except Exception as exc:  # noqa: BLE001 — informational; never crash pages
        logger.warning("Could not read scrape_log for scraped_at: %s", exc)
        return None


def format_scraped_at(value: Optional[datetime]) -> str:
    """Human-readable scraped_at for trust strip; honest unknown if missing."""
    if value is None:
        return UNKNOWN_SCRAPED_AT
    tz = zoneinfo.ZoneInfo(settings.timezone)
    if value.tzinfo is None:
        # ETL timestamps are wall-clock Cape Town / box-local, not UTC.
        local = value.replace(tzinfo=tz)
    else:
        local = value.astimezone(tz)
    return local.strftime("%d %b %Y, %H:%M") + " Africa/Johannesburg"


def get_scraped_at_display(conn: Optional[duckdb.DuckDBPyConnection]) -> str:
    """Human-readable "timetable as of" stamp, or the unknown placeholder."""
    if conn is None:
        return UNKNOWN_SCRAPED_AT
    return format_scraped_at(get_scraped_at_raw(conn))


def trust_context(
    conn: Optional[duckdb.DuckDBPyConnection] = None,
    *,
    show_shedding_trust: bool = False,
) -> dict[str, Any]:
    """Shared Jinja context for trust strip / footer / wake banner."""
    return {
        "scraped_at": get_scraped_at_display(conn),
        "db_ready": is_pool_ready(),
        "show_shedding_trust": show_shedding_trust,
        "myciti_url": MYCITI_URL,
        "uct_transport_url": UCT_TRANSPORT_URL,
        "location_privacy_line": LOCATION_PRIVACY_LINE,
    }
