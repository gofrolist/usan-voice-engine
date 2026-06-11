"""Pure schedule-window math: day masks, quiet-hours intersection, DST-safe next_run_at.

Implements the wall-clock window model of the batch/scheduled-calling spec
(§5.2 schedule-materialization branches, §6.3 quiet-hours enforcement point 1).
All timezone arithmetic is zoneinfo-only — never SQL tz math — per the
correctness note in quiet_hours.py: zoneinfo.ZoneInfo recomputes the UTC offset
lazily from the wall-clock fields on every access, so local wall-clock targets
are built as zoneinfo-aware datetimes and converted with ``.astimezone(UTC)``.
Never reinterpret an instant from another zone via ``.replace(tzinfo=...)`` and
never substitute a pytz bound-offset tzinfo, which would NOT recompute.

Deliberate deviation from spec §9 wording: ``effective_window`` returns ``None``
for an empty intersection rather than raising. The error contract is preserved
one layer up — schema validators turn an empty intersection into a 422 and
``next_run_at`` raises ``ValueError``.

Per-profile policy composition (small-unlocks spec §3.3.3): both functions take
keyword-default ``policy_start``/``policy_end`` bounds, so the effective dialing
interval — window ∩ statutory ∩ policy — is computed in ONE place rather than by
sequential clamps fighting each other. ``next_run_at`` returns ``None`` ONLY for
the policy-induced empty intersection (callers skip observably, rule 2); a
statutory-empty window still raises ``ValueError`` — that contract stays
reserved for misconfiguration caught at save time.
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from usan_api.quiet_hours import QUIET_END_HOUR, QUIET_START_HOUR

DAY_NAMES: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # bit 0 = Mon

_QUIET_START = time(QUIET_START_HOUR)
_QUIET_END = time(QUIET_END_HOUR)
_FULL_MASK = (1 << len(DAY_NAMES)) - 1  # 127 = every day
# Any masked weekday recurs within 7 days; scanning 8 local dates also covers the
# "today is the only masked day but its window already ended" wrap-around case.
_MAX_SCAN_DAYS = 8


def days_to_mask(days: Sequence[str]) -> int:
    """Bitmask for a list of day names (bit 0 = Mon, per ``DAY_NAMES``).

    Order-insensitive; raises ValueError on an empty list or an unknown name.
    """
    if not days:
        raise ValueError("days_of_week must not be empty")
    mask = 0
    for day in days:
        try:
            mask |= 1 << DAY_NAMES.index(day)
        except ValueError:
            raise ValueError(f"unknown day name: {day!r} (expected one of {DAY_NAMES})") from None
    return mask


def mask_to_days(mask: int) -> list[str]:
    """Canonical Mon-first day-name list for a bitmask; ValueError unless 1 <= mask <= 127."""
    if not 1 <= mask <= _FULL_MASK:
        raise ValueError(f"days mask must be in [1, {_FULL_MASK}], got {mask}")
    return [name for bit, name in enumerate(DAY_NAMES) if mask & (1 << bit)]


def effective_window(
    start: time,
    end: time,
    *,
    policy_start: time | None = None,
    policy_end: time | None = None,
) -> tuple[time, time] | None:
    """Schedule window ∩ quiet hours [09:00, 21:00) ∩ policy — wall clock, tz-invariant.

    Returns ``None`` for an empty intersection (callers raise/422 — see module
    docstring). Quiet hours are start-inclusive, end-exclusive, so a window
    ending exactly at 09:00 is empty. ``policy_start``/``policy_end`` are the
    resolved per-profile narrowing bounds (small-unlocks spec §3.3.3) —
    PolicyConfig validates them within the statutory window, so the extra
    ``max``/``min`` against the statutory constants is defensive only; absent
    bounds are zero-diff with the pre-policy behavior.
    """
    eff_start = max(start, _QUIET_START, policy_start or _QUIET_START)
    eff_end = min(end, _QUIET_END, policy_end or _QUIET_END)
    if eff_start >= eff_end:
        return None
    return (eff_start, eff_end)


def _zone(tz_name: str) -> ZoneInfo:
    """Resolve an IANA zone, normalizing failures to ValueError (fail-closed callers)."""
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone: {tz_name!r}") from exc


def _wall(day: date, t: time, tz: ZoneInfo) -> datetime:
    """Zoneinfo-aware local wall-clock instant for ``t`` on ``day``.

    Constructor-attached zoneinfo (PEP 615): the UTC offset is recomputed from
    these wall fields at ``.astimezone(UTC)`` time — correct across DST
    transitions, unlike reinterpreting a foreign instant with ``.replace(tzinfo=...)``.
    """
    return datetime.combine(day, t, tzinfo=tz)


def window_bounds_utc(
    day: date, tz_name: str, *, window_start: time, window_end: time
) -> tuple[datetime, datetime]:
    """Aware-UTC (start, end) of the local wall-clock window on ``day``."""
    tz = _zone(tz_name)
    return (
        _wall(day, window_start, tz).astimezone(UTC),
        _wall(day, window_end, tz).astimezone(UTC),
    )


def day_bounds_utc(day: date, tz_name: str) -> tuple[datetime, datetime]:
    """Aware-UTC [local midnight of ``day``, local midnight of the next day).

    Used by the daily autonomous-call cap: on DST transition days the local day
    is 23 or 25 hours long, and a cached-offset bug here would double-count or
    miss a root call at the cap boundary.
    """
    tz = _zone(tz_name)
    return (
        _wall(day, time.min, tz).astimezone(UTC),
        _wall(day + timedelta(days=1), time.min, tz).astimezone(UTC),
    )


def local_date(at: datetime, tz_name: str) -> date:
    """Elder-local calendar date of an aware instant; ValueError on unknown tz."""
    return at.astimezone(_zone(tz_name)).date()


def next_run_at(
    after: datetime,
    tz_name: str,
    *,
    window_start: time,
    window_end: time,
    days_mask: int,
    policy_start: time | None = None,
    policy_end: time | None = None,
) -> datetime | None:
    """Earliest aware-UTC instant >= ``after`` inside the effective window on a masked day.

    Scans <= 8 local dates; builds local wall-clock targets on zoneinfo-aware
    datetimes then ``.astimezone(UTC)`` (never ``.replace(tzinfo=...)``).
    Raises ValueError on an unknown timezone or a statutory-empty window.
    Returns ``None`` ONLY when the policy bounds empty an otherwise-valid
    statutory intersection (small-unlocks spec §3.3.3 rule 2) — callers skip
    the occurrence/target observably; policy-free callers can never see it.
    """
    window = effective_window(
        window_start, window_end, policy_start=policy_start, policy_end=policy_end
    )
    if window is None:
        if effective_window(window_start, window_end) is None:
            raise ValueError("schedule window never intersects quiet hours [09:00, 21:00)")
        return None  # policy-induced empty intersection (§3.3.3 rule 2)
    if not 1 <= days_mask <= _FULL_MASK:
        raise ValueError(f"days mask must be in [1, {_FULL_MASK}], got {days_mask}")
    eff_start, eff_end = window
    tz = _zone(tz_name)
    local_after = after.astimezone(tz)
    for offset in range(_MAX_SCAN_DAYS):
        day = local_after.date() + timedelta(days=offset)
        if not days_mask & (1 << day.weekday()):
            continue
        start_utc = _wall(day, eff_start, tz).astimezone(UTC)
        if after <= start_utc:
            return start_utc
        if after < _wall(day, eff_end, tz).astimezone(UTC):
            return after.astimezone(UTC)
    raise AssertionError("unreachable: a masked weekday must occur within the 8-day scan")
