"""Per-route validity for UCT shuttle timetables.

UCT publishes timetables in dated sets (e.g. "Consolidation & Exam",
26 Oct - 20 Nov 2026) and some routes end on a different day (Route 5 runs
to 26 Nov). Each route's ``valid_until`` is stored in ``timetables``. Once a
route's end date has passed its times are no longer shown as current: they
are left out of search results and stop boards, and the "Timetable expired"
banner is shown instead.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

CAPE_TOWN = ZoneInfo("Africa/Johannesburg")


def today_cape_town() -> date:
    """Today's date in Cape Town (UCT validity dates are local calendar days)."""
    return datetime.now(CAPE_TOWN).date()


def _as_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def uct_route_validity(conn) -> dict[str, tuple[Optional[date], Optional[date]]]:
    """route_id -> (valid_from, valid_until) for UCT routes (latest end date)."""
    rows = conn.execute(
        "SELECT route_id, MIN(valid_from), MAX(valid_until) FROM timetables "
        "WHERE operator = 'uct' GROUP BY route_id"
    ).fetchall()
    return {r[0]: (_as_date(r[1]), _as_date(r[2])) for r in rows}


def is_expired(valid_until: Optional[date], today: date) -> bool:
    """A route is expired the day after its last valid day."""
    return valid_until is not None and valid_until < today


def expired_routes(
    validity: dict[str, tuple[Optional[date], Optional[date]]], today: date
) -> dict[str, date]:
    """route_id -> valid_until for every route whose end date has passed."""
    return {rid: vu for rid, (_vf, vu) in validity.items() if is_expired(vu, today)}


def _route_label(route_id: str) -> str:
    num = route_id[3:] if route_id.upper().startswith("UCT") else route_id
    return "Overnight library loop" if num.upper() == "OVN" else f"Route {num}"


def expiry_warning(expired: dict[str, date]) -> Optional[str]:
    """Banner text for expired UCT routes, or None if nothing has expired."""
    if not expired:
        return None
    last = max(expired.values())
    labels = sorted(
        (_route_label(r) for r in expired),
        key=lambda s: (not s.startswith("Route "), int(s[6:]) if s[6:].isdigit() else 0, s),
    )
    names = ", ".join(labels)
    return (
        f"UCT shuttle timetable expired on {last.strftime('%d %b %Y')} ({names}). "
        "These times are no longer shown until UCT publishes a new timetable."
    )


def uct_expiry_state(conn, today: Optional[date] = None) -> tuple[set[str], Optional[str]]:
    """(expired route_ids, banner text) — safe on DBs without validity data."""
    today = today or today_cape_town()
    try:
        exp = expired_routes(uct_route_validity(conn), today)
    except Exception:  # pragma: no cover - older DBs without the columns
        return set(), None
    return set(exp), expiry_warning(exp)


def drop_expired(route_ids: Iterable[str], expired: set[str]) -> bool:
    """True if none of the given routes is expired (row may be shown)."""
    return not any(r in expired for r in (route_ids or []))
