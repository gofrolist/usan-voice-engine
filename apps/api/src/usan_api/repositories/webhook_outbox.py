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
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import webhook_events
from usan_api.db.models import WebhookDelivery, WebhookEndpoint

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult


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
    """Pending count for one endpoint (the redeliver/test 100-row enqueue cap, §8.4)."""
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
    ``next_attempt_at = now()`` makes the row immediately due. ``delivered_at``
    resets with the other attempt diagnostics — otherwise a redelivered row
    that later terminally fails keeps a contradictory delivered_at.
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
            delivered_at=None,
        )
        .returning(WebhookDelivery.id)
    )
    return result.scalar_one_or_none()


# --- Worker half (spec §5.2–§5.4): claim lease, guarded outcomes, housekeeping ---


@dataclass(frozen=True, slots=True)
class ClaimedDelivery:
    """One claimed outbox row as plain data — the claim transaction commits
    immediately, so the worker must hold no ORM state and no row locks across
    HTTP POSTs (spec §5.2).
    """

    id: uuid.UUID
    endpoint_id: uuid.UUID
    event: str
    payload: dict[str, Any]
    attempts: int


# Spec §5.2 verbatim, modulo two plan-mandated parametrizations: every now()
# is the bound :now (the time-travel seam the worker tests depend on) and
# LIMIT is the bound :limit (claim_due's keyword). The attempt bump IS the
# lease: the next rung is pre-scheduled at claim time, so a crash anywhere
# after this commit re-offers the row automatically — no reclaim sweeper.
# The JOIN on e.enabled means breaker-/operator-disabled endpoints simply
# stop being claimed; rows resume (attempt count intact) on re-enable.
_CLAIM_SQL = text(
    """
    WITH due AS (
        SELECT d.id, d.next_attempt_at
        FROM webhook_deliveries d
        JOIN webhook_endpoints e ON e.id = d.endpoint_id AND e.enabled
        WHERE d.status = 'pending'
          AND d.next_attempt_at <= :now
          AND d.attempts < 4
        ORDER BY d.next_attempt_at
        LIMIT :limit
        FOR UPDATE OF d SKIP LOCKED
    )
    UPDATE webhook_deliveries w
    SET attempts = w.attempts + 1,
        next_attempt_at = :now + (CASE w.attempts + 1
            WHEN 1 THEN interval '1 minute'
            WHEN 2 THEN interval '5 minutes'
            ELSE interval '30 minutes' END),
        updated_at = :now
    FROM due
    WHERE w.id = due.id
    RETURNING w.id, w.endpoint_id, w.event, w.payload, w.attempts,
              due.next_attempt_at AS due_at
    """
)


async def claim_due(db: AsyncSession, *, now: datetime, limit: int = 20) -> list[ClaimedDelivery]:
    """Claim up to ``limit`` due rows, bumping each onto its next retry rung.

    The caller commits immediately after — the bumped ``next_attempt_at`` is a
    crash-safe lease, not a lock. Returned oldest-due-first (UPDATE..RETURNING
    has no guaranteed order, so the sort happens here on the CTE's original
    ``next_attempt_at``; id tiebreak because fan-out rows share one
    transaction timestamp).
    """
    result = await db.execute(_CLAIM_SQL, {"now": now, "limit": limit})
    rows = sorted(result.mappings().all(), key=lambda row: (row["due_at"], row["id"]))
    return [
        ClaimedDelivery(
            id=row["id"],
            endpoint_id=row["endpoint_id"],
            event=row["event"],
            payload=row["payload"],
            attempts=row["attempts"],
        )
        for row in rows
    ]


async def mark_delivered(db: AsyncSession, delivery_id: uuid.UUID, *, response_code: int) -> bool:
    """Guarded success outcome (spec §5.3): only a pending row can be delivered.

    The status predicate makes the at-least-once race idempotent — a duplicate
    outcome (or a concurrent operator redeliver) claims nothing and returns
    False, leaving the first writer's row untouched.
    """
    result = await db.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == delivery_id, WebhookDelivery.status == "pending")
        .values(status="delivered", delivered_at=func.now(), response_code=response_code)
        .returning(WebhookDelivery.id)
    )
    return result.scalar_one_or_none() is not None


async def mark_attempt_failed(
    db: AsyncSession,
    delivery_id: uuid.UUID,
    *,
    response_code: int | None,
    last_error: str,
    terminal: bool,
) -> bool:
    """Guarded failure outcome (spec §5.3, review L3 — same guard as success).

    Non-terminal: record ``response_code``/``last_error`` and leave the row
    pending — the claim-time lease already scheduled the next rung. Terminal
    (attempt 4): flip to ``failed``. ``last_error`` is the exception TYPE NAME
    only, never ``str(exc)`` (PHI-adjacent rule).
    """
    values: dict[str, Any] = {"response_code": response_code, "last_error": last_error}
    if terminal:
        values["status"] = "failed"
    result = await db.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == delivery_id, WebhookDelivery.status == "pending")
        .values(**values)
        .returning(WebhookDelivery.id)
    )
    return result.scalar_one_or_none() is not None


# Spec §5.4: a crash on a row already at attempts=4 leaves it pending but
# unclaimable (the attempts < 4 predicate) — sweep it to failed after a
# 10-minute grace (a live worker may still be mid-POST). COALESCE so a genuine
# last error type is never overwritten by the sentinel (review L4d).
_SWEEP_SQL = text(
    """
    UPDATE webhook_deliveries
    SET status = 'failed', last_error = COALESCE(last_error, 'crash_residual')
    WHERE status = 'pending' AND attempts >= 4
      AND updated_at < CAST(:now AS timestamptz) - interval '10 minutes'
    """
)

# Spec §5.4: pending rows for disabled endpoints and flag-off backlogs escape
# every other cleanup path — bound outbox growth (and how stale an occurred_at
# a receiver can ever see) to 7 days.
_EXPIRE_SQL = text(
    """
    UPDATE webhook_deliveries
    SET status = 'failed', last_error = 'expired'
    WHERE status = 'pending' AND created_at < CAST(:now AS timestamptz) - interval '7 days'
    """
)

# Spec §5.4: payloads are PHI-free by construction, so this is hygiene, not
# compliance. Pending rows are expire_stale_pending's job, never prune's.
_PRUNE_SQL = text(
    """
    DELETE FROM webhook_deliveries
    WHERE status IN ('delivered', 'failed')
      AND created_at < CAST(:now AS timestamptz) - interval '30 days'
    """
)


async def sweep_crash_residue(db: AsyncSession, *, now: datetime) -> int:
    """Fail crash-orphaned attempts=4 pending rows; returns the swept count."""
    result = cast("CursorResult[Any]", await db.execute(_SWEEP_SQL, {"now": now}))
    return int(result.rowcount or 0)


async def expire_stale_pending(db: AsyncSession, *, now: datetime) -> int:
    """Fail pending rows older than 7 days; returns the expired count."""
    result = cast("CursorResult[Any]", await db.execute(_EXPIRE_SQL, {"now": now}))
    return int(result.rowcount or 0)


async def prune_old(db: AsyncSession, *, now: datetime) -> int:
    """Delete settled rows older than 30 days; returns the pruned count."""
    result = cast("CursorResult[Any]", await db.execute(_PRUNE_SQL, {"now": now}))
    return int(result.rowcount or 0)


async def count_pending(db: AsyncSession) -> int:
    """Total pending backlog — the per-cycle depth the poller reports (spec §9)."""
    result = await db.execute(
        select(func.count()).select_from(WebhookDelivery).where(WebhookDelivery.status == "pending")
    )
    return int(result.scalar_one())
