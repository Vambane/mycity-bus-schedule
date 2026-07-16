"""
ls_ui.py — Pure presentation helpers for load shedding UI
==========================================================
Turns DisruptionAssessment results and shedding windows into the strings,
chip tuples and chart frames app.py renders. No Streamlit imports, so
every branching decision here is testable without a Streamlit runtime.

Every helper returns None (or an empty frame) when there is nothing to
show, so the app's stage 0 rendering stays byte-identical to a build
without load shedding awareness.
"""

import pandas as pd

from disruption import DisruptionAssessment

# Amber chip matching the _chips() tuple shape in app.py: (label, bg, fg)
AFFECTED_CHIP: tuple[str, str, str] = ("⚡ Stage-affected", "#fff4e0", "#b45309")

MAP_BAND_COLOR = "#F59E0B"
MAP_BAND_OPACITY = 0.15


def connection_chip(
    assessment: DisruptionAssessment | None,
) -> tuple[str, str, str] | None:
    """Return the amber badge chip for an affected connection, else None."""
    if assessment is not None and assessment.affected:
        return AFFECTED_CHIP
    return None


def connection_caption(
    assessment: DisruptionAssessment | None,
    from_block: int | None,
    to_block: int | None,
) -> str | None:
    """Describe the affected end(s), block(s) and window(s) of a connection.

    Example: "Load shedding at origin (block 7): 14:00 to 16:30".
    Returns None for unaffected (or absent) assessments.
    """
    if assessment is None or not assessment.affected:
        return None
    block_by_end = {"origin": from_block, "destination": to_block}
    ends = " and ".join(
        f"{end} (block {block_by_end[end]})" for end in assessment.affected_ends
    )
    return f"Load shedding at {ends}: {', '.join(assessment.windows)}"


def adjusted_duration_text(duration_min: int, buffer_min: int) -> str | None:
    """Format the buffered duration, e.g. '~52 min (+10 buffer)'.

    Returns None when there is no buffer, so unaffected cards show only
    their scheduled duration.
    """
    if buffer_min <= 0:
        return None
    return f"~{duration_min + buffer_min} min (+{buffer_min} buffer)"


def map_band_frame(
    windows_hours: list[tuple[float, float]] | None,
    lo: float,
    hi: float,
) -> pd.DataFrame:
    """Clip shedding windows to the map's hour axis as a start/end frame.

    Args:
        windows_hours: (start, end) decimal-hour windows (GTFS axis, may
            exceed 24), or None when the feature is off.
        lo: left edge of the chart's hour domain.
        hi: right edge of the chart's hour domain.

    Returns:
        DataFrame with float columns start/end; empty when nothing shows.
    """
    rows = [
        {"start": max(start, lo), "end": min(end, hi)}
        for start, end in (windows_hours or [])
        if max(start, lo) < min(end, hi)
    ]
    return pd.DataFrame(rows, columns=["start", "end"], dtype=float)


def stop_banner_text(
    stop_name: str,
    block: int | None,
    windows: list[str],
) -> str | None:
    """Warning banner for a stop whose block sheds today, else None."""
    if block is None or not windows:
        return None
    return (
        f"⚡ Load shedding at {stop_name} (block {block}) today: "
        f"{', '.join(windows)}. Departures in these windows may be delayed."
    )
