from datetime import timedelta

import pytest

from usan_api.db.base import CallStatus
from usan_api.retry_policy import next_retry_delay


@pytest.mark.parametrize(
    ("status", "attempt", "expected"),
    [
        (CallStatus.NO_ANSWER, 1, timedelta(minutes=30)),
        (CallStatus.NO_ANSWER, 2, timedelta(hours=2)),
        (CallStatus.NO_ANSWER, 3, None),
        (CallStatus.NO_ANSWER, 4, None),
        (CallStatus.VOICEMAIL_LEFT, 1, timedelta(hours=3)),
        (CallStatus.VOICEMAIL_LEFT, 2, None),
        (CallStatus.BUSY, 1, timedelta(minutes=5)),
        (CallStatus.BUSY, 2, None),
        (CallStatus.FAILED, 1, timedelta(minutes=1)),
        (CallStatus.FAILED, 2, None),
        # out-of-range attempts never produce a delay
        (CallStatus.NO_ANSWER, 0, None),
        (CallStatus.FAILED, 99, None),
    ],
)
def test_next_retry_delay_policy(status, attempt, expected):
    assert next_retry_delay(status, attempt) == expected


@pytest.mark.parametrize(
    "status",
    [
        CallStatus.COMPLETED,
        CallStatus.DNC_BLOCKED,
        CallStatus.CANCELLED,
        CallStatus.QUEUED,
        CallStatus.DIALING,
        CallStatus.RINGING,
        CallStatus.IN_PROGRESS,
    ],
)
def test_non_retryable_statuses_never_retry(status):
    assert next_retry_delay(status, 1) is None
