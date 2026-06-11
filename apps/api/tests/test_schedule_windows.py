"""Pure unit tests for schedule_windows — day masks, quiet-hours intersection,
DST-safe next_run_at (spec §5.2/§6.3). No DB."""

import inspect
from datetime import UTC, date, datetime, time

import pytest

from usan_api.schedule_windows import (
    DAY_NAMES,
    day_bounds_utc,
    days_to_mask,
    effective_window,
    local_date,
    mask_to_days,
    next_run_at,
    window_bounds_utc,
)


def test_days_mask_round_trip():
    assert days_to_mask(["mon"]) == 1
    assert days_to_mask(["mon", "sun"]) == 0b1000001
    assert mask_to_days(127) == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    # round-trip for every single day (bit 0 = Mon)
    for day in DAY_NAMES:
        assert mask_to_days(days_to_mask([day])) == [day]
    # order-insensitive input, canonical-order output
    assert days_to_mask(["sun", "mon"]) == days_to_mask(["mon", "sun"])
    assert mask_to_days(days_to_mask(["fri", "tue"])) == ["tue", "fri"]


def test_days_mask_rejects_unknown_and_empty():
    with pytest.raises(ValueError, match="monday"):
        days_to_mask(["monday"])
    with pytest.raises(ValueError, match="empty"):
        days_to_mask([])
    with pytest.raises(ValueError, match="mask"):
        mask_to_days(0)
    with pytest.raises(ValueError, match="mask"):
        mask_to_days(128)


def test_effective_window_intersects_quiet_hours():
    # partial overlap clamps to quiet-hours start
    assert effective_window(time(8), time(10)) == (time(9), time(10))
    # fully inside quiet hours passes through unchanged
    assert effective_window(time(10), time(12)) == (time(10), time(12))
    # entirely after quiet hours -> empty -> None
    assert effective_window(time(21, 30), time(22, 30)) is None
    # boundary: [09:00, 21:00) is start-inclusive, so a window ending AT 09:00 is empty
    assert effective_window(time(7), time(9)) is None


def test_effective_window_is_timezone_invariant():
    # Pure wall-clock math: the function structurally cannot depend on a timezone
    # (spec §3.1 tz-invariance claim, pinned via the signature itself).
    params = list(inspect.signature(effective_window).parameters)
    assert params == ["start", "end"]


def test_next_run_at_inside_window_returns_after():
    # UTC elder, window 09:00-17:00 all days; 12:00 local is inside -> unchanged
    after = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    result = next_run_at(after, "UTC", window_start=time(9), window_end=time(17), days_mask=127)
    assert result == after


def test_next_run_at_before_start_returns_todays_start():
    after = datetime(2026, 6, 10, 7, 0, tzinfo=UTC)
    result = next_run_at(after, "UTC", window_start=time(9), window_end=time(17), days_mask=127)
    assert result == datetime(2026, 6, 10, 9, 0, tzinfo=UTC)


def test_next_run_at_after_end_skips_to_next_masked_day():
    # 2026-06-08 is a Monday; 18:00 is past the 17:00 window end -> next Monday 09:00
    after = datetime(2026, 6, 8, 18, 0, tzinfo=UTC)
    result = next_run_at(
        after, "UTC", window_start=time(9), window_end=time(17), days_mask=days_to_mask(["mon"])
    )
    assert result == datetime(2026, 6, 15, 9, 0, tzinfo=UTC)


def test_next_run_at_dst_spring_forward_recomputes_offset():
    # after = Sat 2026-03-07 18:00 EST (23:00Z); the 09:00-11:00 window is already over.
    # Next masked day is Sun 2026-03-08 — the US spring-forward date: 09:00 local is
    # EDT (UTC-4) == 13:00Z. A cached EST offset would wrongly yield 14:00Z (the
    # zoneinfo-recompute landmine from quiet_hours.py's correctness note).
    after = datetime(2026, 3, 7, 23, 0, tzinfo=UTC)
    result = next_run_at(
        after,
        "America/New_York",
        window_start=time(9),
        window_end=time(11),
        days_mask=days_to_mask(["sat", "sun"]),
    )
    assert result == datetime(2026, 3, 8, 13, 0, tzinfo=UTC)


def test_next_run_at_dst_fall_back_recomputes_offset():
    # after = Sat 2026-10-31 20:00 EDT (2026-11-01T00:00Z); Saturday is unmasked.
    # Sun 2026-11-01 is the US fall-back date: 09:00 local is EST (UTC-5) == 14:00Z,
    # not 13:00Z EDT — the symmetric landmine.
    after = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    result = next_run_at(
        after,
        "America/New_York",
        window_start=time(9),
        window_end=time(17),
        days_mask=days_to_mask(["sun"]),
    )
    assert result == datetime(2026, 11, 1, 14, 0, tzinfo=UTC)


def test_next_run_at_invalid_tz_raises_value_error():
    # fail-closed contract: an unresolvable zone must never produce a dial instant
    with pytest.raises(ValueError, match="timezone"):
        next_run_at(
            datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
            "Mars/Olympus",
            window_start=time(9),
            window_end=time(17),
            days_mask=127,
        )


def test_next_run_at_empty_effective_window_raises_value_error():
    # the spec §9 error contract lives here (effective_window itself returns None)
    with pytest.raises(ValueError, match="quiet hours"):
        next_run_at(
            datetime(2026, 6, 10, 12, 0, tzinfo=UTC),
            "UTC",
            window_start=time(21, 30),
            window_end=time(22, 30),
            days_mask=127,
        )


def test_window_bounds_utc_and_day_bounds_utc():
    # 2026-06-10 New York is EDT (UTC-4): 09:00->13:00Z, 17:00->21:00Z
    assert window_bounds_utc(
        date(2026, 6, 10), "America/New_York", window_start=time(9), window_end=time(17)
    ) == (datetime(2026, 6, 10, 13, 0, tzinfo=UTC), datetime(2026, 6, 10, 21, 0, tzinfo=UTC))
    # day_bounds_utc returns local-midnight bounds (for the daily cap)
    assert day_bounds_utc(date(2026, 6, 10), "America/New_York") == (
        datetime(2026, 6, 10, 4, 0, tzinfo=UTC),
        datetime(2026, 6, 11, 4, 0, tzinfo=UTC),
    )
    # local_date: 01:00Z on June 10 is still June 9 in New York
    ny_date = local_date(datetime(2026, 6, 10, 1, 0, tzinfo=UTC), "America/New_York")
    assert ny_date == date(2026, 6, 9)


def test_day_bounds_utc_spans_dst_transition_days():
    # fall-back day (2026-11-01) is a 25-hour local day: midnight EDT (04:00Z) to
    # next midnight EST (05:00Z)
    assert day_bounds_utc(date(2026, 11, 1), "America/New_York") == (
        datetime(2026, 11, 1, 4, 0, tzinfo=UTC),
        datetime(2026, 11, 2, 5, 0, tzinfo=UTC),
    )
    # spring-forward day (2026-03-08) is a 23-hour local day: midnight EST (05:00Z)
    # to next midnight EDT (04:00Z)
    assert day_bounds_utc(date(2026, 3, 8), "America/New_York") == (
        datetime(2026, 3, 8, 5, 0, tzinfo=UTC),
        datetime(2026, 3, 9, 4, 0, tzinfo=UTC),
    )
