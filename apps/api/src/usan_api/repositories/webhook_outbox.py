"""Transactional outbox for outbound webhook deliveries (spec §2.1).

Every event corresponds to exactly one guarded state transition that already
commits atomically; the outbox row JOINS THAT SAME COMMIT. This repo only
inserts + flushes in the caller's session — the caller's existing
``db.commit()`` makes business change and event durable together (house rule:
repos flush, never commit — the sms_outbox header documents the same
split for the delivery side). Consequences:

- No phantom events: a rolled-back transition enqueues nothing.
- No lost events: a committed transition has its rows on disk before any
  process can crash.
- Exactly-once enqueue: one guarded transition produces exactly one fan-out.

Fan-out happens at ENQUEUE time: one ``webhook_deliveries`` row per *enabled*
endpoint whose subscription list contains the event. Zero qualifying endpoints
⇒ zero rows ⇒ the whole feature is a no-op at zero cost (the ship-inert
posture; the endpoint table is capped at 10 rows so the SELECT inside hot
mutators stays cheap).
"""

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import webhook_events
from usan_api.db.models import WebhookDelivery, WebhookEndpoint


async def enqueue_event(db: AsyncSession, *, event: str, payload: dict[str, Any]) -> int:
    """Fan out one pending delivery per enabled endpoint subscribed to ``event``.

    Flush-only — joins the caller's transaction. Returns the number of rows
    inserted (0 when no endpoint qualifies: zero rows, no flush).
    """
    result = await db.execute(
        select(WebhookEndpoint.id).where(
            WebhookEndpoint.enabled.is_(True),
            WebhookEndpoint.events.contains([event]),
        )
    )
    endpoint_ids = list(result.scalars().all())
    if not endpoint_ids:
        return 0
    for endpoint_id in endpoint_ids:
        db.add(WebhookDelivery(endpoint_id=endpoint_id, event=event, payload=payload))
    await db.flush()
    return len(endpoint_ids)


async def enqueue_ping(db: AsyncSession, *, endpoint_id: uuid.UUID) -> WebhookDelivery:
    """Insert one ``ping`` delivery row REGARDLESS of subscriptions (spec §4).

    The /test pipeline: ping is deliberately not subscribable (0014 CHECK
    asymmetry), so it bypasses the fan-out filter and targets the endpoint
    directly. ``next_attempt_at`` keeps its ``now()`` server default — the row
    is immediately due. Flush-only; the router commits.
    """
    row = WebhookDelivery(
        endpoint_id=endpoint_id,
        event="ping",
        payload=webhook_events.ping_payload(endpoint_id),
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def pending_counts(db: AsyncSession) -> dict[uuid.UUID, int]:
    """Per-endpoint pending backlog for the endpoint list (spec §4/§9).

    Absent key == zero pending — callers default missing endpoints to 0.
    """
    result = await db.execute(
        select(WebhookDelivery.endpoint_id, func.count())
        .where(WebhookDelivery.status == "pending")
        .group_by(WebhookDelivery.endpoint_id)
    )
    return {endpoint_id: int(count) for endpoint_id, count in result.all()}


async def count_pending_for_endpoint(db: AsyncSession, endpoint_id: uuid.UUID) -> int:
    """Pending count for one endpoint (the redeliver 100-row backpressure cap, §8.4)."""
    result = await db.execute(
        select(func.count())
        .select_from(WebhookDelivery)
        .where(
            WebhookDelivery.endpoint_id == endpoint_id,
            WebhookDelivery.status == "pending",
        )
    )
    return int(result.scalar_one())


async def get_delivery(db: AsyncSession, delivery_id: uuid.UUID) -> WebhookDelivery | None:
    return await db.get(WebhookDelivery, delivery_id)


async def list_deliveries(
    db: AsyncSession,
    *,
    endpoint_id: uuid.UUID,
    status: str | None = None,
    event: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[WebhookDelivery]:
    """Newest-first page for the operator debugging surface (spec §4).

    Ordered ``(created_at, id) DESC`` to match ``idx_webhook_deliveries_endpoint``;
    the id tiebreak is load-bearing because fan-out inserts share one transaction
    timestamp. ``limit`` clamped to 1..100.
    """
    stmt = select(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint_id)
    if status is not None:
        stmt = stmt.where(WebhookDelivery.status == status)
    if event is not None:
        stmt = stmt.where(WebhookDelivery.event == event)
    stmt = (
        stmt.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc())
        .limit(max(1, min(limit, 100)))
        .offset(max(offset, 0))
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def redeliver(db: AsyncSession, delivery_id: uuid.UUID) -> uuid.UUID | None:
    """Guarded SQL reset back into the pipeline (spec §4).

    The status predicate is load-bearing: a Python status check would race the
    poller's in-flight claim of a row that just flipped to pending. Returns the
    id, or None when the row is missing or already pending (router 409s).
    ``next_attempt_at = now()`` makes the row immediately due.
    """
    result = await db.execute(
        update(WebhookDelivery)
        .where(
            WebhookDelivery.id == delivery_id,
            WebhookDelivery.status.in_(("delivered", "failed")),
        )
        .values(
            status="pending",
            attempts=0,
            next_attempt_at=func.now(),
            response_code=None,
            last_error=None,
        )
        .returning(WebhookDelivery.id)
    )
    return result.scalar_one_or_none()
