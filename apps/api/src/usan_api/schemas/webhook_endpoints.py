"""Request/response schemas for operator-registered outbound webhook endpoints (spec §4).

Request side: ``url`` runs the registration-time SSRF gate
(``ssrf_guard.validate_webhook_url``, spec §8.1) on create AND on every PATCH
``url`` — the ValueError surfaces as a 422. ``events`` is normalized to a
sorted de-duplicated list constrained to the closed ``WEBHOOK_EVENTS`` enum
(spec §3.1; 'ping' is deliverable but never subscribable, §6.7).

Response side: the secret appears ONLY on ``WebhookEndpointCreatedResponse``
(the 201 body) — list/detail/PATCH reads use ``WebhookEndpointResponse``,
which has no secret field to leak (spec §8.3). The created response is a
deliberate superset of spec §4's pinned create shape (additionally
``consecutive_failures``, ``disabled_reason``, ``pending_deliveries``,
``updated_at``): all additive fields are PHI-free operator state, and a single
response-model hierarchy beats a second trimmed model (plan executor note 4).
The create path composes ``from_model(e, pending=0)`` + the secret.
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from usan_api import ssrf_guard
from usan_api.db.models import WebhookDelivery, WebhookEndpoint
from usan_api.webhook_events import WEBHOOK_EVENTS

# App-level create cap; 422 above. NOT a DB invariant — two concurrent creates
# can momentarily yield 11 on the rate-limited operator plane (spec §3.1).
MAX_WEBHOOK_ENDPOINTS = 10
# Operator label; PHI-free by convention.
MAX_DESCRIPTION_LENGTH = 500
# Enqueue backpressure, shared by redeliver AND /test: 429 when the endpoint
# already has this many pending rows (bounds re-arm/ping storms from a leaked
# operator key, spec §4/§8.4).
MAX_PENDING_FOR_ENQUEUE = 100


def _normalized_events(v: list[str]) -> list[str]:
    """Sorted de-duplicated subscription list, closed-enum checked (spec §3.1)."""
    if not v:
        raise ValueError("events must contain at least one event")
    unknown = sorted(set(v) - set(WEBHOOK_EVENTS))
    if unknown:
        raise ValueError(
            f"unknown events: {', '.join(unknown)}; "
            f"subscribable events are: {', '.join(WEBHOOK_EVENTS)}"
        )
    return sorted(set(v))


class CreateWebhookEndpointRequest(BaseModel):
    url: str
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    events: list[str]

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        # Registration-time SSRF gate (spec §8.1); ValueError -> 422.
        return ssrf_guard.validate_webhook_url(v)

    @field_validator("events")
    @classmethod
    def _normalize_events(cls, v: list[str]) -> list[str]:
        return _normalized_events(v)


class UpdateWebhookEndpointRequest(BaseModel):
    """PATCH body — every field optional; present fields run the create validators.

    A PATCH ``url`` re-runs the FULL registration-time SSRF gate (spec §8.1) —
    an update is exactly as dangerous as a create.
    """

    url: str | None = None
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_LENGTH)
    events: list[str] | None = None
    enabled: bool | None = None

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str | None) -> str | None:
        return None if v is None else ssrf_guard.validate_webhook_url(v)

    @field_validator("events")
    @classmethod
    def _normalize_events(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _normalized_events(v)


class WebhookEndpointResponse(BaseModel):
    """List/detail/PATCH read shape — NO secret field, ever (spec §8.3)."""

    id: uuid.UUID
    url: str
    description: str | None
    enabled: bool
    events: list[str]
    consecutive_failures: int
    disabled_reason: str | None
    pending_deliveries: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, e: WebhookEndpoint, pending: int) -> WebhookEndpointResponse:
        return cls(
            id=e.id,
            url=e.url,
            description=e.description,
            enabled=e.enabled,
            events=e.events,
            consecutive_failures=e.consecutive_failures,
            disabled_reason=e.disabled_reason,
            pending_deliveries=pending,
            created_at=e.created_at,
            updated_at=e.updated_at,
        )


class WebhookEndpointCreatedResponse(WebhookEndpointResponse):
    """201 create body — the ONLY place the secret ever appears (spec §4).

    Built by the create route as ``cls(**from_model(e, pending=0).model_dump(),
    secret=secret)``; the inherited ``from_model`` alone cannot construct this
    class (the secret is required by design).
    """

    secret: str


class WebhookDeliveryResponse(BaseModel):
    """Delivery row for GET /{id}/deliveries (spec §4).

    ``updated_at`` doubles as the last-attempt timestamp; ``payload`` is
    included because it is PHI-free by construction (spec §6).
    """

    id: uuid.UUID
    event: str
    status: str
    attempts: int
    next_attempt_at: datetime
    response_code: int | None
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime
    payload: dict[str, Any]

    @classmethod
    def from_model(cls, d: WebhookDelivery) -> WebhookDeliveryResponse:
        return cls(
            id=d.id,
            event=d.event,
            status=d.status,
            attempts=d.attempts,
            next_attempt_at=d.next_attempt_at,
            response_code=d.response_code,
            last_error=d.last_error,
            delivered_at=d.delivered_at,
            created_at=d.created_at,
            updated_at=d.updated_at,
            payload=d.payload,
        )


class EnqueuedDeliveryResponse(BaseModel):
    """202 body for POST /test and POST /redeliver."""

    delivery_id: uuid.UUID
