from datetime import timedelta

import pytest

from usan_api.db.base import CallStatus
from usan_api.retry_policy import MAX_CHAIN_ATTEMPTS, next_retry_delay
from usan_api.schemas.agent_config import RetryMaxAttempts

_RETRYABLE = [
    CallStatus.NO_ANSWER,
    CallStatus.VOICEMAIL_LEFT,
    CallStatus.BUSY,
    CallStatus.FAILED,
]


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


# --- Per-profile policy generalization (spec 2026-06-10 §3.3.1) ---


@pytest.mark.parametrize(
    ("status", "attempt", "expected"),
    [
        (CallStatus.NO_ANSWER, 1, timedelta(minutes=30)),
        (CallStatus.NO_ANSWER, 2, timedelta(hours=2)),
        (CallStatus.NO_ANSWER, 3, None),
        (CallStatus.VOICEMAIL_LEFT, 1, timedelta(hours=3)),
        (CallStatus.VOICEMAIL_LEFT, 2, None),
        (CallStatus.BUSY, 1, timedelta(minutes=5)),
        (CallStatus.BUSY, 2, None),
        (CallStatus.FAILED, 1, timedelta(minutes=1)),
        (CallStatus.FAILED, 2, None),
        (CallStatus.COMPLETED, 1, None),
        (CallStatus.COMPLETED, 99, None),
        (CallStatus.DNC_BLOCKED, 1, None),
        (CallStatus.DNC_BLOCKED, 99, None),
    ],
)
def test_defaults_reproduce_v1_ladder_exactly(status, attempt, expected):
    """Zero-diff pin: no kwargs == today's hardcoded v1 ladder, byte-for-byte."""
    assert next_retry_delay(status, attempt) == expected


def test_multiplier_scales_every_rung():
    assert next_retry_delay(CallStatus.NO_ANSWER, 1, delay_multiplier=2.0) == timedelta(minutes=60)
    assert next_retry_delay(CallStatus.NO_ANSWER, 2, delay_multiplier=2.0) == timedelta(hours=4)
    assert next_retry_delay(CallStatus.BUSY, 1, delay_multiplier=0.5) == timedelta(
        minutes=2, seconds=30
    )


@pytest.mark.parametrize("status", _RETRYABLE)
@pytest.mark.parametrize("attempt", [1, 2, 3, 4, 5])
def test_max_attempts_zero_disables(status, attempt):
    assert next_retry_delay(status, attempt, max_attempts=0) is None


def test_max_attempts_extends_with_final_rung_repeat():
    # Past the ladder, the final rung repeats up to the policy cap.
    assert next_retry_delay(CallStatus.NO_ANSWER, 3, max_attempts=4) == timedelta(hours=2)
    assert next_retry_delay(CallStatus.NO_ANSWER, 4, max_attempts=4) == timedelta(hours=2)
    assert next_retry_delay(CallStatus.NO_ANSWER, 5, max_attempts=4) is None
    assert next_retry_delay(CallStatus.BUSY, 2, max_attempts=3) == timedelta(minutes=5)
    assert next_retry_delay(CallStatus.BUSY, 3, max_attempts=3) == timedelta(minutes=5)


def test_multiplier_scales_final_rung_extension():
    # Interaction pin: attempts past the ladder repeat the FINAL rung, and the
    # multiplier scales that repeated rung too — extension and scaling compose,
    # neither resets the other. NO_ANSWER's final rung is 2h; ×2.0 → 4h on both
    # the in-ladder rung and every policy-extended repeat, until the cap stops.
    assert next_retry_delay(
        CallStatus.NO_ANSWER, 3, max_attempts=4, delay_multiplier=2.0
    ) == timedelta(hours=4)
    assert next_retry_delay(
        CallStatus.NO_ANSWER, 4, max_attempts=4, delay_multiplier=2.0
    ) == timedelta(hours=4)
    assert next_retry_delay(CallStatus.NO_ANSWER, 5, max_attempts=4, delay_multiplier=2.0) is None
    # Sub-1.0 multiplier on a single-rung ladder's extension (BUSY: 5m → 2m30s).
    assert next_retry_delay(CallStatus.BUSY, 3, max_attempts=3, delay_multiplier=0.5) == timedelta(
        minutes=2, seconds=30
    )


def test_mixed_status_chain_global_semantics():
    """§3.3.1 pin: max_attempts keys on the CHAIN-GLOBAL attempt number.

    A BUSY outcome at attempt 3 (attempts 1-2 were NO_ANSWER) retries under
    ``busy: 3`` — there are no per-status counters. Under the builtin (busy: 1)
    the same outcome stops.
    """
    assert next_retry_delay(CallStatus.BUSY, 3, max_attempts=3) == timedelta(minutes=5)
    assert next_retry_delay(CallStatus.BUSY, 3) is None


def test_max_chain_attempts_single_source():
    """Chain-length invariant: root + 4 retries, 4 being the RetryMaxAttempts le= bound.

    Read the ceiling from the field metadata so raising ``le=`` in
    schemas/agent_config.py without raising MAX_CHAIN_ATTEMPTS fails here
    (reintroducing the depth-4 chain-tip escape, spec §3.3.1).
    """
    assert MAX_CHAIN_ATTEMPTS == 5
    le_bounds = {
        next(m.le for m in field.metadata if getattr(m, "le", None) is not None)
        for field in RetryMaxAttempts.model_fields.values()
    }
    assert len(le_bounds) == 1
    assert 1 + le_bounds.pop() == MAX_CHAIN_ATTEMPTS
