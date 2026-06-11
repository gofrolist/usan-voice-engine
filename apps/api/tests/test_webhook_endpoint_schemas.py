"""Pure unit tests for schemas/webhook_endpoints (spec §3.1/§4/§8.1).

Pins the request-side contracts — events normalized to a sorted de-duplicated
closed-enum list ('ping' not subscribable), the schema→ssrf_guard.validate_webhook_url
wiring (full URL matrix lives in test_ssrf_guard.py), the 500-char description cap,
PATCH re-running the full SSRF gate — and the response-side secret posture: the
secret field exists ONLY on the 201 create response model. No DB.
"""

import pytest
from pydantic import ValidationError


def _valid_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "url": "https://hooks.example.com/usan",
        "events": ["call.completed"],
    }
    return {**base, **overrides}


def test_create_normalizes_events():
    from usan_api.schemas.webhook_endpoints import CreateWebhookEndpointRequest

    req = CreateWebhookEndpointRequest(
        **_valid_kwargs(events=["call.completed", "call.started", "call.completed"])
    )
    # De-duplicated AND sorted (spec §3.1: the DB CHECK alone would admit duplicates).
    assert req.events == ["call.completed", "call.started"]


def test_create_rejects_unknown_event():
    from usan_api.schemas.webhook_endpoints import CreateWebhookEndpointRequest

    with pytest.raises(ValidationError, match="call.exploded"):
        CreateWebhookEndpointRequest(**_valid_kwargs(events=["call.exploded"]))


def test_create_rejects_ping_subscription():
    from usan_api.schemas.webhook_endpoints import CreateWebhookEndpointRequest

    # 'ping' is deliverable (the /test pipeline) but never subscribable (spec §6.7).
    with pytest.raises(ValidationError, match="ping"):
        CreateWebhookEndpointRequest(**_valid_kwargs(events=["ping"]))


def test_create_rejects_empty_events():
    from usan_api.schemas.webhook_endpoints import CreateWebhookEndpointRequest

    with pytest.raises(ValidationError):
        CreateWebhookEndpointRequest(**_valid_kwargs(events=[]))


def test_create_url_runs_ssrf_validator():
    from usan_api.schemas.webhook_endpoints import CreateWebhookEndpointRequest

    # The full URL matrix lives in test_ssrf_guard.py; this pins the
    # schema -> ssrf_guard.validate_webhook_url wiring (spec §8.1).
    with pytest.raises(ValidationError, match="internal hostname"):
        CreateWebhookEndpointRequest(**_valid_kwargs(url="https://metadata.google.internal/"))
    with pytest.raises(ValidationError, match="https"):
        CreateWebhookEndpointRequest(**_valid_kwargs(url="http://x.example.com"))

    ok = CreateWebhookEndpointRequest(**_valid_kwargs(url="https://hooks.example.com:8443/p"))
    assert ok.url == "https://hooks.example.com:8443/p"


def test_description_capped_500():
    from usan_api.schemas.webhook_endpoints import (
        MAX_DESCRIPTION_LENGTH,
        CreateWebhookEndpointRequest,
    )

    assert MAX_DESCRIPTION_LENGTH == 500

    with pytest.raises(ValidationError, match="500"):
        CreateWebhookEndpointRequest(**_valid_kwargs(description="x" * 501))

    ok = CreateWebhookEndpointRequest(**_valid_kwargs(description="x" * 500))
    assert ok.description == "x" * 500
    assert CreateWebhookEndpointRequest(**_valid_kwargs()).description is None


def test_update_all_optional_and_url_revalidated():
    from usan_api.schemas.webhook_endpoints import UpdateWebhookEndpointRequest

    # Empty PATCH body is valid — every field optional.
    empty = UpdateWebhookEndpointRequest()
    assert empty.url is None
    assert empty.description is None
    assert empty.events is None
    assert empty.enabled is None

    # PATCH url re-runs the FULL registration-time SSRF gate (spec §8.1):
    # IP literals rejected outright.
    with pytest.raises(ValidationError, match="IP literal"):
        UpdateWebhookEndpointRequest(url="https://10.0.0.1/")

    # Same event validators when present: normalized + closed enum.
    upd = UpdateWebhookEndpointRequest(events=["flag.created", "call.started", "flag.created"])
    assert upd.events == ["call.started", "flag.created"]
    with pytest.raises(ValidationError, match="ping"):
        UpdateWebhookEndpointRequest(events=["ping"])


def test_responses_have_no_secret_field():
    from usan_api.schemas.webhook_endpoints import (
        WebhookEndpointCreatedResponse,
        WebhookEndpointResponse,
    )

    # The 201 create response is the ONLY shape that ever carries the secret
    # (spec §4/§8.3); list/detail/PATCH reads can never leak it because the
    # field does not exist on their model.
    assert "secret" not in WebhookEndpointResponse.model_fields
    assert "secret" in WebhookEndpointCreatedResponse.model_fields
