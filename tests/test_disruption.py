"""
test_disruption.py — Tests for the load shedding disruption model
==================================================================
Golden-value checks of the generated CCT 16-block schedule matrix, window
computation (including the midnight spill-over), and connection
assessment (stage 0 short-circuit, NULL blocks, buffers, overnight GTFS
times, boundary semantics).
"""

from datetime import date, time

from disruption import (
    DisruptionAssessment,
    MAX_BUFFER_MIN,
    PER_END_BUFFER_MIN,
    _blocks_for_slot,
    assess_connection,
    get_shedding_windows,
    get_window_strings,
    get_windows_hours,
)

# Dates chosen so the day of month is what the test needs.
DAY_1 = date(2026, 3, 1)    # day 1; previous day is Feb 28 (day 28)
DAY_2 = date(2026, 3, 2)    # day 2; previous day is day 1
DAY_17 = date(2026, 3, 17)  # day 17: repeats day 1's pattern


# ---------------------------------------------------------------------------
# Schedule matrix
# ---------------------------------------------------------------------------

def test_matrix_golden_values_stage_1() -> None:
    """Known (day, slot) combinations shed the expected block at stage 1."""
    assert _blocks_for_slot(1, 0, 1) == {1}     # day 1, 00:00
    assert _blocks_for_slot(1, 11, 1) == {12}   # day 1, 22:00
    assert _blocks_for_slot(2, 0, 1) == {13}    # day 2 continues the rotation


def test_matrix_day_17_repeats_day_1() -> None:
    """The rotation wraps after 16 days: day 17 equals day 1."""
    for slot in range(12):
        for stage in (1, 4, 8):
            assert _blocks_for_slot(17, slot, stage) == _blocks_for_slot(1, slot, stage)


def test_matrix_stage_2_adds_offset_8() -> None:
    """Stage 2 sheds the stage 1 block plus the block 8 positions on."""
    assert _blocks_for_slot(1, 0, 2) == {1, 9}


def test_matrix_stages_nest_and_stage_8_sheds_8_blocks() -> None:
    """Each stage's blocks are a superset of the previous stage's."""
    for day in (1, 7, 16):
        for slot in (0, 5, 11):
            previous: set[int] = set()
            for stage in range(1, 9):
                blocks = _blocks_for_slot(day, slot, stage)
                assert len(blocks) == stage
                assert previous <= blocks
                previous = blocks


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def test_stage_zero_yields_no_windows() -> None:
    """Stage 0 never sheds anything."""
    assert not get_shedding_windows(1, 0, DAY_1)
    assert not get_windows_hours(1, 0, DAY_1)
    assert not get_window_strings(1, 0, DAY_1)


def test_window_is_two_and_a_half_hours() -> None:
    """Block 1 sheds day 1 slot 0: a single 00:00 to 02:30 window."""
    assert get_shedding_windows(1, 1, DAY_1) == [(time(0, 0), time(2, 30))]


def test_midnight_spill_from_previous_day() -> None:
    """Day 1's 22:00 slot (block 12) shows on day 2 as 00:00 to 00:30."""
    assert get_shedding_windows(12, 1, DAY_2) == [(time(0, 0), time(0, 30))]


def test_stage_8_sheds_every_other_slot() -> None:
    """At stage 8 a block sheds in 6 of the 12 daily slots."""
    windows = get_shedding_windows(1, 8, DAY_1)
    assert len(windows) == 6
    assert windows[0] == (time(0, 0), time(2, 30))


def test_window_string_wraps_past_midnight() -> None:
    """A 22:00 slot formats as '22:00 to 00:30' (wall-clock wrap)."""
    assert "22:00 to 00:30" in get_window_strings(12, 1, DAY_1)


def test_windows_hours_span_past_24() -> None:
    """Decimal-hour windows keep the GTFS convention: 22:00 ends at 24.5."""
    assert (22.0, 24.5) in get_windows_hours(12, 1, DAY_1)


# ---------------------------------------------------------------------------
# assess_connection
# ---------------------------------------------------------------------------

def test_assess_stage_zero_short_circuits() -> None:
    """Stage 0 is never affected, whatever the blocks and times."""
    result = assess_connection("01:00:00", "01:30:00", 1, 1, 0, DAY_1)
    assert result == DisruptionAssessment()


def test_assess_null_blocks_never_flagged() -> None:
    """Unknown blocks are never flagged, individually or together."""
    inside = "01:00:00"  # inside block 1's day-1 window at stage 1
    both_none = assess_connection(inside, inside, None, None, 1, DAY_1)
    assert not both_none.affected
    origin_none = assess_connection(inside, "12:00:00", None, 1, 1, DAY_1)
    assert not origin_none.affected


def test_assess_origin_only_buffer() -> None:
    """Only the origin inside a window: one end, one buffer unit."""
    result = assess_connection("01:00:00", "12:00:00", 1, 1, 1, DAY_1)
    assert result.affected
    assert result.affected_ends == ["origin"]
    assert result.delay_buffer_min == PER_END_BUFFER_MIN
    assert result.windows == ["00:00 to 02:30"]


def test_assess_both_ends_buffer_capped() -> None:
    """Both ends affected: buffer is 2 x per-end, capped at the maximum."""
    result = assess_connection("01:00:00", "01:30:00", 1, 1, 1, DAY_1)
    assert result.affected_ends == ["origin", "destination"]
    assert result.delay_buffer_min == min(2 * PER_END_BUFFER_MIN, MAX_BUFFER_MIN)
    assert result.delay_buffer_min == 20


def test_assess_overnight_departure_uses_next_day() -> None:
    """A 24:15 departure on day 1 is checked against day 2's schedule."""
    # Day 2 slot 0 sheds block 13; block 13 sheds nowhere on day 1 itself.
    result = assess_connection("24:15:00", "25:00:00", 13, None, 1, DAY_1)
    assert result.affected
    assert result.affected_ends == ["origin"]
    assert result.windows == ["00:00 to 02:30"]


def test_assess_boundary_semantics_half_open() -> None:
    """Exactly at a window start is affected; exactly at its end is not."""
    at_start = assess_connection("00:00:00", "12:00:00", 1, None, 1, DAY_1)
    assert at_start.affected
    at_end = assess_connection("02:30:00", "12:00:00", 1, None, 1, DAY_1)
    assert not at_end.affected
