"""§5.3 retry policy (v1 hardcoded).

A retry's delay is keyed on the terminal status and the attempt number that just
ended. ``None`` means stop retrying. Pure function — no I/O, no clock.
"""

from datetime import timedelta

from usan_api.db.base import CallStatus


def next_retry_delay(status: CallStatus, attempt: int) -> timedelta | None:
    """Delay before the next attempt, or None when the policy says stop."""
    if status is CallStatus.NO_ANSWER:
        if attempt == 1:
            return timedelta(minutes=30)
        if attempt == 2:
            return timedelta(hours=2)
        return None
    if status is CallStatus.VOICEMAIL_LEFT:
        return timedelta(hours=3) if attempt == 1 else None
    if status is CallStatus.BUSY:
        return timedelta(minutes=5) if attempt == 1 else None
    if status is CallStatus.FAILED:
        return timedelta(minutes=1) if attempt == 1 else None
    return None
