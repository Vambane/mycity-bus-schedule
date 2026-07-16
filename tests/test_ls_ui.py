"""
test_ls_ui.py — Tests for the load shedding presentation helpers
=================================================================
The helpers are pure (no Streamlit), so every chip, caption, banner and
chart-band decision is asserted directly. The stage 0 / feature-off path
must always come back as None or empty so the app renders identically to
a build without load shedding awareness.
"""

from disruption import DisruptionAssessment
from ls_ui import (
    AFFECTED_CHIP,
    adjusted_duration_text,
    connection_caption,
    connection_chip,
    map_band_frame,
    stop_banner_text,
)

UNAFFECTED = DisruptionAssessment()
ORIGIN_HIT = DisruptionAssessment(True, ["origin"], ["14:00 to 16:30"], 10)
BOTH_HIT = DisruptionAssessment(
    True, ["origin", "destination"], ["14:00 to 16:30", "18:00 to 20:30"], 20
)


# ---------------------------------------------------------------------------
# Chips
# ---------------------------------------------------------------------------

def test_chip_none_when_unaffected_or_absent() -> None:
    """No chip for unaffected connections or when the feature is off."""
    assert connection_chip(UNAFFECTED) is None
    assert connection_chip(None) is None


def test_chip_tuple_when_affected() -> None:
    """An affected connection gets the amber chip in _chips() shape."""
    chip = connection_chip(ORIGIN_HIT)
    assert chip == AFFECTED_CHIP
    label, bg, fg = chip
    assert label == "⚡ Stage-affected"
    assert bg.startswith("#") and fg.startswith("#")


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------

def test_caption_none_when_unaffected_or_absent() -> None:
    """No caption when nothing is affected."""
    assert connection_caption(UNAFFECTED, 1, 2) is None
    assert connection_caption(None, 1, 2) is None


def test_caption_names_end_block_and_window() -> None:
    """The caption names the affected end, its block, and the window."""
    caption = connection_caption(ORIGIN_HIT, 7, 3)
    assert caption == "Load shedding at origin (block 7): 14:00 to 16:30"


def test_caption_lists_both_ends() -> None:
    """Both affected ends appear, each with its own block."""
    caption = connection_caption(BOTH_HIT, 7, 3)
    assert "origin (block 7)" in caption
    assert "destination (block 3)" in caption
    assert "14:00 to 16:30" in caption and "18:00 to 20:30" in caption


# ---------------------------------------------------------------------------
# Adjusted duration
# ---------------------------------------------------------------------------

def test_adjusted_duration_none_without_buffer() -> None:
    """No buffer means no adjusted duration text at all."""
    assert adjusted_duration_text(42, 0) is None


def test_adjusted_duration_adds_buffer() -> None:
    """The text shows scheduled + buffer and names the buffer size."""
    assert adjusted_duration_text(42, 10) == "~52 min (+10 buffer)"
    assert adjusted_duration_text(42, 20) == "~62 min (+20 buffer)"


# ---------------------------------------------------------------------------
# Map bands
# ---------------------------------------------------------------------------

def test_map_band_frame_empty_for_none_or_empty() -> None:
    """Feature off (None) or no windows: an empty frame, no bands drawn."""
    assert map_band_frame(None, 5.0, 23.0).empty
    assert map_band_frame([], 5.0, 23.0).empty


def test_map_band_frame_clips_to_domain() -> None:
    """Windows are clipped to the axis and out-of-range ones dropped."""
    frame = map_band_frame([(4.0, 6.5), (22.0, 24.5), (30.0, 32.5)], 5.0, 23.0)
    assert frame.to_dict("records") == [
        {"start": 5.0, "end": 6.5},
        {"start": 22.0, "end": 23.0},
    ]


def test_map_band_frame_keeps_overnight_band_when_axis_allows() -> None:
    """A 22:00 to 24:30 window survives intact on a late-running axis."""
    frame = map_band_frame([(22.0, 24.5)], 5.0, 25.0)
    assert frame.to_dict("records") == [{"start": 22.0, "end": 24.5}]


# ---------------------------------------------------------------------------
# Stop banner
# ---------------------------------------------------------------------------

def test_banner_none_without_block_or_windows() -> None:
    """Unknown block or a shed-free day produces no banner."""
    assert stop_banner_text("Civic Centre", None, ["14:00 to 16:30"]) is None
    assert stop_banner_text("Civic Centre", 7, []) is None


def test_banner_names_stop_block_and_windows() -> None:
    """The banner names the stop, its block, and every window."""
    banner = stop_banner_text("Civic Centre", 7, ["14:00 to 16:30", "22:00 to 00:30"])
    assert "Civic Centre" in banner
    assert "block 7" in banner
    assert "14:00 to 16:30" in banner and "22:00 to 00:30" in banner
