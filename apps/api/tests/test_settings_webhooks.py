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


def test_webhook_delivery_defaults_are_inert():
    # Ship-inert contract (spec §5.1/§11): with defaults nothing egresses —
    # the delivery half of the poller is gated off and stays off until a
    # deploy explicitly sets WEBHOOK_DELIVERY_ENABLED=true.
    s = Settings(**_BASE)
    assert s.webhook_delivery_enabled is False
    assert s.webhook_delivery_poll_interval_s == 10
    assert s.webhook_delivery_timeout_s == 10
    assert s.webhook_delivery_circuit_breaker_threshold == 10


@pytest.mark.parametrize(
    "overrides",
    [
        {"WEBHOOK_DELIVERY_POLL_INTERVAL_S": "4"},  # lt 5
        {"WEBHOOK_DELIVERY_POLL_INTERVAL_S": "301"},  # gt 300
        {"WEBHOOK_DELIVERY_TIMEOUT_S": "0"},  # lt 1
        {"WEBHOOK_DELIVERY_TIMEOUT_S": "61"},  # gt 60
        {"WEBHOOK_DELIVERY_CIRCUIT_BREAKER_THRESHOLD": "0"},  # lt 1
        {"WEBHOOK_DELIVERY_CIRCUIT_BREAKER_THRESHOLD": "101"},  # gt 100
    ],
)
def test_webhook_delivery_bounds_enforced(overrides: dict[str, str]):
    with pytest.raises(ValidationError):
        Settings(**_BASE, **overrides)


def test_flag_on_alone_is_valid():
    # Per-row endpoint secrets mean the flag has no startup prerequisite —
    # there is deliberately NO cross-field validator (spec §5.1). Pinned so
    # nobody adds one: flag-on with nothing else configured must construct.
    s = Settings(**_BASE, WEBHOOK_DELIVERY_ENABLED="true")
    assert s.webhook_delivery_enabled is True


def test_namespace_disjoint_from_inbound_webhook_max_age():
    # WEBHOOK_DELIVERY_* (outbound) is deliberately disjoint from the inbound
    # LiveKit-verification WEBHOOK_MAX_AGE_S — one Settings instance carries
    # both, and flipping the outbound flag never perturbs the inbound knob.
    s = Settings(**_BASE, WEBHOOK_DELIVERY_ENABLED="true")
    assert s.webhook_max_age_s == 300
    assert s.webhook_delivery_enabled is True
    assert s.webhook_delivery_poll_interval_s == 10
    assert s.webhook_delivery_timeout_s == 10
    assert s.webhook_delivery_circuit_breaker_threshold == 10
