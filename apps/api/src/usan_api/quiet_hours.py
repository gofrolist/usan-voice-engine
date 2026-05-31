"""TCPA quiet-hours clamping for retry scheduling (§5.3, §10).

A retry may only be placed within ``[09:00, 21:00)`` in the elder's local time.
``next_allowed`` returns the earliest aware-UTC instant >= ``dt_utc`` inside that
window. An invalid IANA timezone raises ValueError so callers can fail CLOSED
(never risk an out-of-hours call) rather than guessing.

Correctness note: zoneinfo.ZoneInfo is a *rule* object — it recomputes the UTC
offset lazily from the wall-clock fields on every access, so building the target
local wall time with ``.replace(hour=9, ...)`` and then ``.astimezone(UTC)`` yields
the correct EST/EDT offset for that date. This is true ONLY for zoneinfo; never
attach a zone with ``.replace(tzinfo=...)`` and never substitute a pytz
bound-offset tzinfo, which would NOT recompute.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

QUIET_START_HOUR = 9  # calls allowed from 09:00 local (inclusive)
QUIET_END_HOUR = 21  # calls not allowed at/after 21:00 local (exclusive)


def next_allowed(dt_utc: datetime, tz_name: str) -> datetime:
    """Earliest aware-UTC instant >= dt_utc within [09:00, 21:00) local time.

    ``dt_utc`` must be timezone-aware. Raises ValueError for an unknown timezone.
    """
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone: {tz_name!r}") from exc

    local = dt_utc.astimezone(tz)
    if QUIET_START_HOUR <= local.hour < QUIET_END_HOUR:
        return dt_utc
    if local.hour < QUIET_START_HOUR:
        target = local.replace(hour=QUIET_START_HOUR, minute=0, second=0, microsecond=0)
    else:  # at/after QUIET_END_HOUR -> next local morning
        target = (local + timedelta(days=1)).replace(
            hour=QUIET_START_HOUR, minute=0, second=0, microsecond=0
        )
    return target.astimezone(UTC)
