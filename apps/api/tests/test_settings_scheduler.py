import pytest
from pydantic import ValidationError

from usan_api.settings import Settings

_BASE = {
    "DATABASE_URL": "postgresql://u:p@localhost/db",
    "LIVEKIT_API_KEY": "k",
    "LIVEKIT_API_SECRET": "a" * 32,
    "LIVEKIT_URL": "ws://livekit:7880",
    "JWT_SIGNING_KEY": "s" * 32,
    "OPERATOR_API_KEY": "o" * 32,
}


def test_scheduler_defaults_are_inert():
    # Ship-inert contract (spec §5.1/§10.1): with defaults, no materialization
    # happens and the retry poller's claim behavior is bit-identical to today.
    s = Settings(**_BASE)
    assert s.scheduler_poller_enabled is False
    assert s.concurrency_gate_enabled is False
    assert s.autonomous_dialing_paused is False
    assert s.scheduler_poll_interval_s == 60
    assert s.scheduler_batch_size == 50
    assert s.max_concurrent_calls == 8
    assert s.reserved_concurrency == 2
    assert s.max_autonomous_calls_per_elder_per_day == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"SCHEDULER_POLL_INTERVAL_S": "10"},  # lt 15
        {"SCHEDULER_POLL_INTERVAL_S": "601"},  # gt 600
        {"SCHEDULER_BATCH_SIZE": "0"},  # lt 1
        {"MAX_CONCURRENT_CALLS": "51"},  # gt 50
        {"MAX_AUTONOMOUS_CALLS_PER_ELDER_PER_DAY": "0"},  # lt 1
    ],
)
def test_scheduler_bounds_enforced(overrides: dict[str, str]):
    with pytest.raises(ValidationError):
        Settings(**_BASE, **overrides)


def test_reserved_must_be_below_max():
    # reserved >= max means the gate budget (max - reserved - in_flight) can
    # never go positive: the autonomous planes would silently never dial.
    with pytest.raises(ValidationError) as exc_info:
        Settings(**_BASE, RESERVED_CONCURRENCY="8", MAX_CONCURRENT_CALLS="8")
    msg = str(exc_info.value)
    assert "RESERVED_CONCURRENCY" in msg
    assert "MAX_CONCURRENT_CALLS" in msg
