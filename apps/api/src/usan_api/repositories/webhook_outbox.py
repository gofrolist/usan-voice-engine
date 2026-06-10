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

from sqlalchemy import select
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
