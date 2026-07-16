"""
disruption.py — Load shedding disruption model for journeys
============================================================
Pure logic, no UI and no database: given a load shedding stage, a City of
Cape Town block number, and a date, work out when that block is shed and
whether a specific bus connection is affected.

Schedule source: the standard City of Cape Town 16-block rotational
schedule (the Eskom incident schedule the city publishes as its
"Load-shedding: all areas schedule" table). Its whole matrix is generated
from three small pieces of data:

  - 12 daily time slots starting every 2 hours (00:00, 02:00, ... 22:00),
    each outage lasting 2 h 30 (so a slot spills 30 min into the next);
  - a base block that advances by one every slot, so
    base = ((day_of_month - 1) * 12 + slot) mod 16 and day 17 repeats
    day 1's pattern;
  - fixed per-stage offsets: stage n sheds the blocks at
    base + offset for the first n entries of (0, 8, 12, 4, 2, 10, 14, 6).

Overnight convention: times are GTFS-style HH:MM:SS strings where the
hour may reach 24+ (see journey.py). A departure at 24:15:00 on service
date D happens at 00:15 on D+1 and is checked against D+1's schedule.
Each end of a connection is checked as a point in time (the moment the
bus is at that stop), not the whole ride span.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta, time

from journey import _to_minutes  # deliberately shared: one time convention

# ---------------------------------------------------------------------------
# Schedule matrix (as data)
# ---------------------------------------------------------------------------

SLOT_STARTS_MIN: tuple[int, ...] = tuple(h * 60 for h in range(0, 24, 2))
OUTAGE_MIN = 150  # each outage runs 2 h 30 from its slot start
STAGE_OFFSETS: tuple[int, ...] = (0, 8, 12, 4, 2, 10, 14, 6)  # stages 1..8

MAX_STAGE = len(STAGE_OFFSETS)
N_BLOCKS = 16

# Buffer heuristic: traffic lights are down around a shedding stop, so
# allow extra minutes per affected end of the journey, capped overall.
PER_END_BUFFER_MIN = 10
MAX_BUFFER_MIN = 20

DAY_MIN = 1440  # minutes in a day


# ---------------------------------------------------------------------------
# Window computation
# ---------------------------------------------------------------------------

def _blocks_for_slot(day_of_month: int, slot: int, stage: int) -> set[int]:
    """Return the set of blocks (1..16) shed in a slot at a given stage."""
    stage = min(stage, MAX_STAGE)
    if stage <= 0:
        return set()
    base = ((day_of_month - 1) * 12 + slot) % N_BLOCKS
    return {(base + off) % N_BLOCKS + 1 for off in STAGE_OFFSETS[:stage]}


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or touching (start, end) intervals, sorted."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _windows_min(block: int, stage: int, on_date: date) -> list[tuple[int, int]]:
    """Shedding windows for a block as minutes relative to on_date's midnight.

    Covers the range [0, 2880) (the service date plus the following day,
    matching GTFS 24+ hour times) and includes the spill-over of the
    previous day's 22:00 slot into the early morning. Intervals are merged
    and clipped to the range.

    Args:
        block: CCT block number 1..16.
        stage: load shedding stage; 0 or below yields no windows.
        on_date: the service date the minute offsets are relative to.

    Returns:
        Sorted, non-overlapping (start_min, end_min) tuples, half-open.
    """
    if stage <= 0:
        return []
    horizon = 2 * DAY_MIN
    intervals: list[tuple[int, int]] = []
    for day_offset in (-1, 0, 1):
        day = (on_date + timedelta(days=day_offset)).day
        for slot, slot_start in enumerate(SLOT_STARTS_MIN):
            if block in _blocks_for_slot(day, slot, stage):
                start = day_offset * DAY_MIN + slot_start
                end = start + OUTAGE_MIN
                if end > 0 and start < horizon:
                    intervals.append((max(start, 0), min(end, horizon)))
    return _merge(intervals)


def get_shedding_windows(block: int, stage: int, on_date: date) -> list[tuple[time, time]]:
    """Return a block's shedding windows within a calendar date.

    Windows are clipped to the date itself: the previous night's 22:00
    slot shows up as (00:00, 00:30), and a window running to midnight is
    clipped to 23:59 so both ends stay within the date.

    Args:
        block: CCT block number 1..16.
        stage: load shedding stage; 0 or below yields no windows.
        on_date: the calendar date to report windows for.

    Returns:
        List of (start, end) datetime.time pairs, ascending.
    """
    windows = []
    for start, end in _windows_min(block, stage, on_date):
        start, end = max(start, 0), min(end, DAY_MIN - 1)
        if start < end:
            windows.append(
                (time(start // 60, start % 60), time(end // 60, end % 60))
            )
    return windows


def get_windows_hours(block: int, stage: int, on_date: date) -> list[tuple[float, float]]:
    """Return shedding windows as decimal hours over 0..48 for charting.

    A 22:00 slot comes back as (22.0, 24.5); values above 24 belong to the
    following day, matching the GTFS-style hour axis of the departure map.
    """
    return [(s / 60, e / 60) for s, e in _windows_min(block, stage, on_date)]


def _fmt_window(start_min: int, end_min: int) -> str:
    """Format a minute interval as a wall-clock range like '14:00 to 16:30'."""
    def hhmm(minutes: int) -> str:
        minutes %= DAY_MIN
        return f"{minutes // 60:02d}:{minutes % 60:02d}"
    return f"{hhmm(start_min)} to {hhmm(end_min)}"


def get_window_strings(block: int, stage: int, on_date: date) -> list[str]:
    """Human-readable shedding windows starting within the given date."""
    return [
        _fmt_window(start, end)
        for start, end in _windows_min(block, stage, on_date)
        if start < DAY_MIN
    ]


# ---------------------------------------------------------------------------
# Connection assessment
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DisruptionAssessment:
    """Outcome of checking one connection against the shedding schedule.

    Attributes:
        affected: True when at least one end is inside a shedding window.
        affected_ends: subset of ["origin", "destination"], in that order.
        windows: human-readable windows that caused the flags.
        delay_buffer_min: suggested extra minutes for the journey.
    """
    affected: bool = False
    affected_ends: list[str] = field(default_factory=list)
    windows: list[str] = field(default_factory=list)
    delay_buffer_min: int = 0


def _hit(moment_min: int, windows: list[tuple[int, int]]) -> tuple[int, int] | None:
    """Return the window containing the moment, if any (half-open check)."""
    for start, end in windows:
        if start <= moment_min < end:
            return (start, end)
    return None


def assess_connection(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    dep_time: str,
    arr_time: str,
    from_block: int | None,
    to_block: int | None,
    stage: int,
    on_date: date,
) -> DisruptionAssessment:
    """Assess whether a connection is disrupted by load shedding.

    Each end is checked as a point in time against its own block's
    windows; an end with an unknown (None) block is never flagged.
    Boundary semantics are half-open: a moment exactly at a window start
    is affected, exactly at its end is not.

    Args:
        dep_time: GTFS departure at the origin stop (HH:MM:SS, hour 24+ ok).
        arr_time: GTFS arrival at the destination stop.
        from_block: origin stop's block, or None when unknown.
        to_block: destination stop's block, or None when unknown.
        stage: effective load shedding stage (0 short-circuits).
        on_date: the service date the times are relative to.

    Returns:
        A DisruptionAssessment; unaffected connections carry a 0 buffer.
    """
    if stage <= 0:
        return DisruptionAssessment()

    ends: list[str] = []
    windows: list[str] = []
    for end_name, block, moment in (
        ("origin", from_block, dep_time),
        ("destination", to_block, arr_time),
    ):
        if block is None:
            continue
        hit = _hit(_to_minutes(moment), _windows_min(block, stage, on_date))
        if hit is not None:
            ends.append(end_name)
            label = _fmt_window(*hit)
            if label not in windows:
                windows.append(label)

    if not ends:
        return DisruptionAssessment()
    buffer_min = min(len(ends) * PER_END_BUFFER_MIN, MAX_BUFFER_MIN)
    return DisruptionAssessment(True, ends, windows, buffer_min)
