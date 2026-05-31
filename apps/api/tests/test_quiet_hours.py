from datetime import UTC, datetime

import pytest

from usan_api.quiet_hours import QUIET_END_HOUR, QUIET_START_HOUR, next_allowed


def test_quiet_hour_constants():
    assert QUIET_START_HOUR == 9
    assert QUIET_END_HOUR == 21


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        # before the window -> same-day 09:00
        (datetime(2026, 5, 31, 6, 0, tzinfo=UTC), datetime(2026, 5, 31, 9, 0, tzinfo=UTC)),
        # exactly 09:00 is inside (START inclusive) -> unchanged
        (datetime(2026, 5, 31, 9, 0, tzinfo=UTC), datetime(2026, 5, 31, 9, 0, tzinfo=UTC)),
        # inside the window -> unchanged
        (datetime(2026, 5, 31, 13, 0, tzinfo=UTC), datetime(2026, 5, 31, 13, 0, tzinfo=UTC)),
        # 20:59 still inside -> unchanged
        (datetime(2026, 5, 31, 20, 59, tzinfo=UTC), datetime(2026, 5, 31, 20, 59, tzinfo=UTC)),
        # exactly 21:00 is outside (END exclusive) -> next-day 09:00
        (datetime(2026, 5, 31, 21, 0, tzinfo=UTC), datetime(2026, 6, 1, 9, 0, tzinfo=UTC)),
        # after the window -> next-day 09:00
        (datetime(2026, 5, 31, 23, 0, tzinfo=UTC), datetime(2026, 6, 1, 9, 0, tzinfo=UTC)),
    ],
)
def test_next_allowed_utc_boundaries(base, expected):
    assert next_allowed(base, "UTC") == expected


def test_next_allowed_returns_aware_datetime():
    result = next_allowed(datetime(2026, 5, 31, 6, 0, tzinfo=UTC), "UTC")
    assert result.tzinfo is not None
    assert result.utcoffset() is not None


def test_next_allowed_eastern_before_window_uses_edt_offset():
    # 2026-03-09 is after US spring-forward (2026-03-08): America/New_York is EDT (UTC-4).
    # 06:00 UTC == 02:00 EDT (before 09:00) -> 09:00 EDT == 13:00 UTC.
    base = datetime(2026, 3, 9, 6, 0, tzinfo=UTC)
    assert next_allowed(base, "America/New_York") == datetime(2026, 3, 9, 13, 0, tzinfo=UTC)


def test_next_allowed_eastern_before_window_uses_est_offset():
    # 2026-11-02 is after US fall-back (2026-11-01): America/New_York is EST (UTC-5).
    # 06:00 UTC == 01:00 EST (before 09:00) -> 09:00 EST == 14:00 UTC.
    base = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
    assert next_allowed(base, "America/New_York") == datetime(2026, 11, 2, 14, 0, tzinfo=UTC)


def test_next_allowed_eastern_after_window_rolls_to_next_local_morning():
    # 2026-03-10 02:00 UTC == 2026-03-09 22:00 EDT (>= 21:00) -> next-day 09:00 EDT == 13:00 UTC.
    base = datetime(2026, 3, 10, 2, 0, tzinfo=UTC)
    assert next_allowed(base, "America/New_York") == datetime(2026, 3, 10, 13, 0, tzinfo=UTC)


def test_next_allowed_eastern_inside_window_unchanged():
    # 2026-03-09 17:00 UTC == 13:00 EDT (inside) -> unchanged.
    base = datetime(2026, 3, 9, 17, 0, tzinfo=UTC)
    assert next_allowed(base, "America/New_York") == base


@pytest.mark.parametrize("bad_tz", ["Not/AZone", "", "Mars/Phobos"])
def test_next_allowed_invalid_timezone_raises(bad_tz):
    with pytest.raises(ValueError, match="timezone"):
        next_allowed(datetime(2026, 5, 31, 6, 0, tzinfo=UTC), bad_tz)
