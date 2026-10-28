"""Static UCT route footnotes from the Consolidation & Exam timetable PDFs.

Plain text only (autoescaped in templates; never marked safe). Shown next to
each affected route wherever its times are shown.
"""
from typing import Iterable, Optional

WEEKEND = ("saturday", "sunday")

# route_id -> list of (day types the note applies to, or None for all, text)
UCT_ROUTE_NOTES: dict[str, list[tuple[Optional[tuple[str, ...]], str]]] = {
    "UCT1": [(("weekday",), "Route 1: Claremont departures from 18:55 on weekdays leave from Roscommon House.")],
    "UCT2": [(None, "Route 2: runs via Kramer after 13:25.")],
    "UCT6": [(None, "Route 6: runs via Mowbray at 06:40 and 07:07.")],
    "UCT11": [(None, "Route 11: only goes via South Stop from 12:25 to 17:40.")],
    "UCT13": [(WEEKEND, "Routes 13/14 at weekends, 15:35–21:50: board and alight at Roscommon House.")],
    "UCT14": [(WEEKEND, "Routes 13/14 at weekends, 15:35–21:50: board and alight at Roscommon House.")],
    "UCTOVN": [(None, "Overnight library loop: UCT student or staff card, or a visitor's pass, needed.")],
}


def uct_notes(route_ids, day_type: Optional[str] = None) -> list[str]:
    """Footnotes for the given route id(s) on a day type, de-duplicated."""
    if isinstance(route_ids, str):
        route_ids = [route_ids]
    out: list[str] = []
    for rid in route_ids or []:
        for days, text in UCT_ROUTE_NOTES.get(rid, []):
            if days is None or day_type is None or day_type in days:
                if text not in out:
                    out.append(text)
    return out
