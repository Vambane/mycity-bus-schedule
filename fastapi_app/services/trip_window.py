"""When the rider wants to travel: leave now, depart at a time, or arrive by a time.

Search results are DataFrames with `dep` and `arr` columns ("HH:MM:SS",
running past 24:00 for overnight trips). This module turns the rider's
choice into a filter on those rows, so the search route stays a thin
caller and the rules can be tested on their own.

  * now     - departures from the current time (the default)
  * depart  - departures at or after the chosen time
  * arrive  - trips that arrive at or before the chosen time, latest
              departure first, so the first card is "the last bus that
              still gets you there". On today's timetable, buses that
              have already left are dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import pandas as pd

NOW, DEPART, ARRIVE = "now", "depart", "arrive"
MODES = (NOW, DEPART, ARRIVE)

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class TripWindow:
    """The rider's choice. `at` is "HH:MM:00" for depart/arrive, None for now."""

    mode: str = NOW
    at: Optional[str] = None

    @property
    def at_hhmm(self) -> str:
        """The chosen time as HH:MM, for display and form values."""
        return self.at[:5] if self.at else ""


def parse_window(when: Optional[str], at: Optional[str]) -> TripWindow:
    """Read the `when` and `at` query parameters.

    Anything unusable (unknown mode, missing or malformed time) falls back
    to "leave now" rather than an error: a half-filled form still searches.
    """
    mode = (when or NOW).strip().lower()
    if mode not in MODES or mode == NOW:
        return TripWindow()
    match = _HHMM.match((at or "").strip())
    if not match:
        return TripWindow()
    return TripWindow(mode, f"{int(match.group(1)):02d}:{match.group(2)}:00")


def arriving_by(df: pd.DataFrame, at: str, not_before: str = "00:00:00") -> pd.DataFrame:
    """Trips arriving at or before *at*, departing at or after *not_before*.

    Latest departure first. Both bounds are stored-time strings, which
    compare correctly as text (zero-padded HH:MM:SS).
    """
    if df is None or df.empty:
        return df
    keep = df[(df["arr"] <= at) & (df["dep"] >= not_before)]
    return keep.sort_values(["dep", "arr"], ascending=[False, True]).reset_index(drop=True)


def last_departure_by(df: pd.DataFrame, at: str) -> Optional[str]:
    """The latest departure that arrives by *at*, ignoring the clock ("HH:MM:SS")."""
    if df is None or df.empty:
        return None
    fits = df[df["arr"] <= at]
    return None if fits.empty else str(fits["dep"].max())
