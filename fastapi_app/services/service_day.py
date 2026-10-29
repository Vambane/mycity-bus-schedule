"""Which timetable applies on a given date, in Cape Town.

MyCiTi and the UCT shuttles publish three timetables: weekday, Saturday
and Sunday. South African public holidays run the Sunday timetable
(MyCiTi: "Sundays and public holidays"; UCT: "Weekends & Public
Holidays"). This module is the single place that maps a calendar date to
one of those, so the search page, the stop board and the "tomorrow
morning" fallback all agree.

Public holidays follow the Public Holidays Act 36 of 1994:
  * ten fixed dates, plus Good Friday and Family Day (Easter Monday);
  * a holiday that falls on a Sunday moves the holiday to the Monday.
One-off days the President declares (election days, for example) are
not predictable, so they are listed by hand in EXTRA_HOLIDAYS.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

WEEKDAY = "weekday"
SATURDAY = "saturday"
SUNDAY = "sunday"

# Fixed-date public holidays: (month, day) -> name.
_FIXED_HOLIDAYS: dict[tuple[int, int], str] = {
    (1, 1): "New Year's Day",
    (3, 21): "Human Rights Day",
    (4, 27): "Freedom Day",
    (5, 1): "Workers' Day",
    (6, 16): "Youth Day",
    (8, 9): "National Women's Day",
    (9, 24): "Heritage Day",
    (12, 16): "Day of Reconciliation",
    (12, 25): "Christmas Day",
    (12, 26): "Day of Goodwill",
}

# One-off holidays declared by the President, which no rule can predict.
# Add them here when gazetted: date -> name.
EXTRA_HOLIDAYS: dict[date, str] = {}


def easter_sunday(year: int) -> date:
    """Western (Gregorian) Easter Sunday, by the anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # names follow the published algorithm
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def public_holidays(year: int) -> dict[date, str]:
    """All South African public holidays in *year*, including Sunday carry-overs."""
    easter = easter_sunday(year)
    holidays: dict[date, str] = {
        date(year, month, day): name for (month, day), name in _FIXED_HOLIDAYS.items()
    }
    holidays[easter - timedelta(days=2)] = "Good Friday"
    holidays[easter + timedelta(days=1)] = "Family Day"

    # A holiday on a Sunday makes the following Monday a public holiday.
    for day, name in list(holidays.items()):
        if day.weekday() == 6:
            monday = day + timedelta(days=1)
            holidays.setdefault(monday, f"{name} (observed)")

    holidays.update({d: n for d, n in EXTRA_HOLIDAYS.items() if d.year == year})
    return holidays


def holiday_name(day: date) -> Optional[str]:
    """The public holiday on *day*, or None."""
    return public_holidays(day.year).get(day)


def day_type_for(day: date) -> str:
    """Timetable for a calendar date: weekday, saturday or sunday.

    Public holidays run the Sunday timetable, whatever day they fall on.
    """
    if holiday_name(day) or day.weekday() == 6:
        return SUNDAY
    if day.weekday() == 5:
        return SATURDAY
    return WEEKDAY


def next_service_day(now: datetime) -> tuple[date, str]:
    """Tomorrow's date and the timetable it runs."""
    tomorrow = now.date() + timedelta(days=1)
    return tomorrow, day_type_for(tomorrow)


def is_live_board(day_type: str, now: datetime, picked_date: Optional[date] = None) -> bool:
    """Whether to hide departures that have already left.

    Only when showing today's own timetable for today. Someone browsing
    Saturday's timetable on a Wednesday night, or planning for a picked
    date, wants the whole day from the first bus.
    """
    if picked_date is not None and picked_date != now.date():
        return False
    return day_type == day_type_for(now.date())


DAY_LABELS = {WEEKDAY: "weekday", SATURDAY: "Saturday", SUNDAY: "Sunday"}
