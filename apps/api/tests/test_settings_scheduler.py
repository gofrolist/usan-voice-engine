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
    assert s.max_autonomous_calls_per_contact_per_day == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"SCHEDULER_POLL_INTERVAL_S": "10"},  # lt 15
        {"SCHEDULER_POLL_INTERVAL_S": "601"},  # gt 600
        {"SCHEDULER_BATCH_SIZE": "0"},  # lt 1
        {"MAX_CONCURRENT_CALLS": "51"},  # gt 50
        {"MAX_AUTONOMOUS_CALLS_PER_CONTACT_PER_DAY": "0"},  # lt 1
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


def test_scheduler_without_gate_rejected():
    # Staged-enable invariant (spec §10.3): the scheduler must never run
    # without the hard dial cap — gate first, scheduler second.
    with pytest.raises(ValidationError) as exc_info:
        Settings(**_BASE, SCHEDULER_POLLER_ENABLED="true", CONCURRENCY_GATE_ENABLED="false")
    msg = str(exc_info.value)
    assert "SCHEDULER_POLLER_ENABLED" in msg
    assert "CONCURRENCY_GATE_ENABLED" in msg


def test_scheduler_gate_valid_combinations_accepted():
    # Both shipped configs stay valid: dev compose (both on), prod (both off);
    # gate-only is the documented staged-enable intermediate state (spec §10.3).
    both_on = Settings(**_BASE, SCHEDULER_POLLER_ENABLED="true", CONCURRENCY_GATE_ENABLED="true")
    assert both_on.scheduler_poller_enabled is True
    assert both_on.concurrency_gate_enabled is True

    gate_only = Settings(**_BASE, CONCURRENCY_GATE_ENABLED="true")
    assert gate_only.scheduler_poller_enabled is False
    assert gate_only.concurrency_gate_enabled is True

    both_off = Settings(**_BASE)
    assert both_off.scheduler_poller_enabled is False
    assert both_off.concurrency_gate_enabled is False
