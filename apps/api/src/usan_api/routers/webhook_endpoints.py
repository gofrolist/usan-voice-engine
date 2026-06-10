"""Operator API for OUTBOUND webhook endpoints + deliveries (spec §4).

Distinct from routers/webhooks.py (the INBOUND LiveKit webhook receiver).
Everything here lives on the operator-key plane (`OPERATOR_API_KEY` bearer,
the batches precedent) — these routes configure machine-to-machine egress,
not human triage. Both prefixes are on the rate-limit allowlist (§8.4).

Audit: every mutation writes an ``admin_audit_log`` row IN THE SAME COMMIT as
the mutation itself, with the sentinel ``actor_email="operator-api-key"`` —
deliberately a durable DB audit (not the batches log-line `_audit`) because
egress configuration changes are worth keeping (spec §4; the sentinel is
documented on the AdminAuditLog model). Audit detail carries ids and changed
field NAMES only — never the secret, never the URL.

Secret handling (spec §8.3): the server-generated secret appears exactly once,
in the 201 create body; no read/list/PATCH response and no log line ever
carries it. Nothing in this module logs at all.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import webhook_signing
from usan_api.auth import require_operator_token
from usan_api.db.models import WebhookEndpoint
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit as audit_repo
from usan_api.repositories import webhook_endpoints as endpoints_repo
from usan_api.repositories import webhook_outbox as outbox_repo
from usan_api.schemas.webhook_endpoints import (
    MAX_PENDING_FOR_ENQUEUE,
    MAX_WEBHOOK_ENDPOINTS,
    CreateWebhookEndpointRequest,
    EnqueuedDeliveryResponse,
    UpdateWebhookEndpointRequest,
    WebhookDeliveryResponse,
    WebhookEndpointCreatedResponse,
    WebhookEndpointResponse,
)
from usan_api.settings import Settings, get_settings

# Sentinel actor for the operator-key plane — durable DB audit for egress
# configuration changes (spec §4). See the AdminAuditLog model docstring.
OPERATOR_ACTOR = "operator-api-key"

router = APIRouter(
    prefix="/v1/webhook-endpoints",
    tags=["webhook-endpoints"],
    dependencies=[Depends(require_operator_token)],
)
deliveries_router = APIRouter(
    prefix="/v1/webhook-deliveries",
    tags=["webhook-endpoints"],
    dependencies=[Depends(require_operator_token)],
)


async def _audit(
    db: AsyncSession,
    *,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    detail: dict[str, Any] | None = None,
) -> None:
    """Same-commit sentinel audit row: ids + field names only (spec §4)."""
    await audit_repo.record(
        db,
        actor_email=OPERATOR_ACTOR,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        detail=detail or {},
    )


async def _get_endpoint_or_404(db: AsyncSession, endpoint_id: uuid.UUID) -> WebhookEndpoint:
    endpoint = await endpoints_repo.get_endpoint(db, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="webhook endpoint not found")
    return endpoint


@router.post("", status_code=201, response_model=WebhookEndpointCreatedResponse)
async def create_endpoint(
    body: CreateWebhookEndpointRequest,
    db: AsyncSession = Depends(get_db),
) -> WebhookEndpointCreatedResponse:
    if await endpoints_repo.count_endpoints(db) >= MAX_WEBHOOK_ENDPOINTS:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint cap reached ({MAX_WEBHOOK_ENDPOINTS}); delete one first",
        )
    secret = webhook_signing.generate_secret()
    endpoint = await endpoints_repo.create_endpoint(
        db, url=body.url, description=body.description, events=body.events, secret=secret
    )
    await _audit(
        db,
        action="webhook_endpoint_created",
        entity_type="webhook_endpoint",
        entity_id=endpoint.id,
        detail={"events": body.events},
    )
    await db.commit()
    # The 201 body is the ONLY place the secret ever appears (spec §4/§8.3).
    return WebhookEndpointCreatedResponse(
        **WebhookEndpointResponse.from_model(endpoint, pending=0).model_dump(), secret=secret
    )


@router.get("", response_model=list[WebhookEndpointResponse])
async def list_endpoints(db: AsyncSession = Depends(get_db)) -> list[WebhookEndpointResponse]:
    endpoints = await endpoints_repo.list_endpoints(db)
    pending = await outbox_repo.pending_counts(db)
    return [WebhookEndpointResponse.from_model(e, pending.get(e.id, 0)) for e in endpoints]


@router.get("/{endpoint_id}", response_model=WebhookEndpointResponse)
async def get_endpoint(
    endpoint_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> WebhookEndpointResponse:
    endpoint = await _get_endpoint_or_404(db, endpoint_id)
    pending = await outbox_repo.count_pending_for_endpoint(db, endpoint_id)
    return WebhookEndpointResponse.from_model(endpoint, pending)


@router.patch("/{endpoint_id}", response_model=WebhookEndpointResponse)
async def update_endpoint(
    endpoint_id: uuid.UUID,
    body: UpdateWebhookEndpointRequest,
    db: AsyncSession = Depends(get_db),
) -> WebhookEndpointResponse:
    endpoint = await _get_endpoint_or_404(db, endpoint_id)
    present = body.model_dump(exclude_unset=True)
    if body.url is not None:
        endpoint.url = body.url  # re-validated through the FULL SSRF gate (schema, §8.1)
    if "description" in present:
        endpoint.description = body.description
    if body.events is not None:
        endpoint.events = body.events
    if body.enabled is True:
        # Operator re-arm: resets consecutive_failures and clears disabled_reason (§5.5).
        await endpoints_repo.reenable(db, endpoint)
    elif body.enabled is False:
        # Operator disable keeps disabled_reason NULL — distinguishable from the
        # breaker's 'circuit_breaker' (spec §3.3).
        endpoint.enabled = False
        endpoint.disabled_reason = None
    await _audit(
        db,
        action="webhook_endpoint_updated",
        entity_type="webhook_endpoint",
        entity_id=endpoint.id,
        detail={"changed": sorted(present)},
    )
    await db.commit()
    # Re-load: updated_at is SQL-side (onupdate=func.now()), so the flushed
    # UPDATE leaves it expired — touching it un-refreshed would lazy-load
    # outside the request's greenlet context (the batches cancel precedent).
    await db.refresh(endpoint)
    pending = await outbox_repo.count_pending_for_endpoint(db, endpoint_id)
    return WebhookEndpointResponse.from_model(endpoint, pending)


@router.delete("/{endpoint_id}", status_code=204)
async def delete_endpoint(endpoint_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> None:
    endpoint = await _get_endpoint_or_404(db, endpoint_id)
    # Delivery rows — including any pending backlog — cascade with it (spec §4).
    await endpoints_repo.delete_endpoint(db, endpoint)
    await _audit(
        db,
        action="webhook_endpoint_deleted",
        entity_type="webhook_endpoint",
        entity_id=endpoint_id,
    )
    await db.commit()


@router.post("/{endpoint_id}/test", status_code=202, response_model=EnqueuedDeliveryResponse)
async def send_test_ping(  # NOT `test_*`: ruff PT028 would read it as a pytest test
    endpoint_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> EnqueuedDeliveryResponse:
    if not settings.webhook_delivery_enabled:
        # A test that can never send is a lie (spec §4).
        raise HTTPException(
            status_code=409, detail="webhook delivery is disabled (WEBHOOK_DELIVERY_ENABLED)"
        )
    endpoint = await _get_endpoint_or_404(db, endpoint_id)
    if not endpoint.enabled:
        raise HTTPException(status_code=409, detail="endpoint is disabled")
    # Same backpressure as redeliver (§8.4): a ping is one more enqueue, so a
    # leaked operator key must not grow an unbounded backlog through /test.
    if await outbox_repo.count_pending_for_endpoint(db, endpoint.id) >= MAX_PENDING_FOR_ENQUEUE:
        raise HTTPException(
            status_code=429, detail="endpoint already has too many pending deliveries"
        )
    # The ping row rides the REAL pipeline: outbox, claim, SSRF re-check,
    # signing, retry ladder (spec §4).
    delivery = await outbox_repo.enqueue_ping(db, endpoint_id=endpoint.id)
    await _audit(
        db,
        action="webhook_test_sent",
        entity_type="webhook_endpoint",
        entity_id=endpoint.id,
        detail={"delivery_id": str(delivery.id)},
    )
    await db.commit()
    return EnqueuedDeliveryResponse(delivery_id=delivery.id)


@router.get("/{endpoint_id}/deliveries", response_model=list[WebhookDeliveryResponse])
async def list_endpoint_deliveries(
    endpoint_id: uuid.UUID,
    status: str | None = None,
    event: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> list[WebhookDeliveryResponse]:
    await _get_endpoint_or_404(db, endpoint_id)
    # The repository clamps limit to 1..100; payloads are PHI-free by
    # construction (§6), which is what makes returning them safe.
    deliveries = await outbox_repo.list_deliveries(
        db, endpoint_id=endpoint_id, status=status, event=event, limit=limit, offset=offset
    )
    return [WebhookDeliveryResponse.from_model(d) for d in deliveries]


@deliveries_router.post(
    "/{delivery_id}/redeliver", status_code=202, response_model=EnqueuedDeliveryResponse
)
async def redeliver_delivery(
    delivery_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> EnqueuedDeliveryResponse:
    delivery = await outbox_repo.get_delivery(db, delivery_id)
    if delivery is None:
        raise HTTPException(status_code=404, detail="delivery not found")
    endpoint = await endpoints_repo.get_endpoint(db, delivery.endpoint_id)
    if endpoint is None or not endpoint.enabled:
        raise HTTPException(status_code=409, detail="endpoint is disabled")
    # Backpressure: bounds re-arm storms from a leaked operator key (§4/§8.4).
    if await outbox_repo.count_pending_for_endpoint(db, endpoint.id) >= MAX_PENDING_FOR_ENQUEUE:
        raise HTTPException(
            status_code=429, detail="endpoint already has too many pending deliveries"
        )
    # Guarded SQL reset — the status predicate races the poller safely (§4).
    reset_id = await outbox_repo.redeliver(db, delivery_id)
    if reset_id is None:
        raise HTTPException(status_code=409, detail="delivery is already pending")
    await _audit(
        db,
        action="webhook_redelivered",
        entity_type="webhook_delivery",
        entity_id=delivery_id,
        detail={"endpoint_id": str(endpoint.id)},
    )
    await db.commit()
    return EnqueuedDeliveryResponse(delivery_id=delivery_id)
