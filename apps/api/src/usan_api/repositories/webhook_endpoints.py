"""Operator-registered webhook endpoints: CRUD + the per-endpoint circuit breaker.

House rule: every function takes the request session and flushes only — the
router (or the delivery worker's own session) commits.

The breaker mutators are ATOMIC SQL, never ORM read-modify-write (spec §5.5,
review L2): under READ COMMITTED an ORM read-modify-write racing a concurrent
``PATCH enabled=true`` reset would lose one of the two writes and could
double-fire the trip WARN/metric. Instead:

- ``increment_failures`` bumps SQL-side (``consecutive_failures =
  consecutive_failures + 1``) guarded on ``enabled`` — a disabled endpoint
  accrues nothing;
- ``trip_breaker`` is a guarded one-shot (``WHERE ... AND enabled RETURNING
  id``) so exactly one concurrent caller observes True — the WARN log and the
  auto-disabled metric fire exactly once per trip (spec §10.11);
- ``reenable`` (the operator re-arm path) writes absolute values
  (``enabled=true, consecutive_failures=0, disabled_reason=NULL``) so it wins
  as last writer in any interleaving with the breaker.

``disabled_reason`` stays NULL on operator disables and reads
``'circuit_breaker'`` on auto-disables — the two states are distinguishable
(spec §3.3). The endpoint ``secret`` is returned once at create and never
logged (spec §8.3); nothing in this module logs at all.
"""

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import WebhookEndpoint


async def create_endpoint(
    db: AsyncSession,
    *,
    url: str,
    description: str | None,
    events: list[str],
    secret: str,
) -> WebhookEndpoint:
    row = WebhookEndpoint(url=url, description=description, events=events, secret=secret)
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def get_endpoint(db: AsyncSession, endpoint_id: uuid.UUID) -> WebhookEndpoint | None:
    return await db.get(WebhookEndpoint, endpoint_id)


async def list_endpoints(db: AsyncSession) -> list[WebhookEndpoint]:
    # Unbounded SELECT is fine here: the table is app-capped at 10 rows (spec §3.1).
    result = await db.execute(
        select(WebhookEndpoint).order_by(WebhookEndpoint.created_at, WebhookEndpoint.id)
    )
    return list(result.scalars().all())


async def count_endpoints(db: AsyncSession) -> int:
    result = await db.execute(select(func.count()).select_from(WebhookEndpoint))
    return int(result.scalar_one())


async def delete_endpoint(db: AsyncSession, endpoint: WebhookEndpoint) -> None:
    # Delivery rows — including any pending backlog — go with it (FK CASCADE,
    # the long-disabled-endpoint cleanup path, spec §4).
    await db.delete(endpoint)
    await db.flush()


async def reenable(db: AsyncSession, endpoint: WebhookEndpoint) -> None:
    """Operator re-arm (``PATCH enabled=true``): reset the breaker entirely.

    Absolute writes, so in a race with ``trip_breaker`` the re-arm wins as
    last writer and the one-shot trip guard is re-armed (spec §5.5/§10.11).
    """
    await db.execute(
        update(WebhookEndpoint)
        .where(WebhookEndpoint.id == endpoint.id)
        .values(enabled=True, consecutive_failures=0, disabled_reason=None)
    )
    await db.flush()
    await db.refresh(endpoint)


async def increment_failures(db: AsyncSession, endpoint_id: uuid.UUID) -> int | None:
    """Atomic failure bump; returns the new count, or None if disabled/missing.

    UPDATE webhook_endpoints SET consecutive_failures = consecutive_failures + 1
    WHERE id = :id AND enabled RETURNING consecutive_failures   (spec §5.5)
    """
    result = await db.execute(
        update(WebhookEndpoint)
        .where(WebhookEndpoint.id == endpoint_id, WebhookEndpoint.enabled.is_(True))
        .values(consecutive_failures=WebhookEndpoint.consecutive_failures + 1)
        .returning(WebhookEndpoint.consecutive_failures)
    )
    value = result.scalar_one_or_none()
    return None if value is None else int(value)


async def reset_failures(db: AsyncSession, endpoint_id: uuid.UUID) -> None:
    """Success path: consecutive means consecutive (spec §5.5)."""
    await db.execute(
        update(WebhookEndpoint)
        .where(WebhookEndpoint.id == endpoint_id)
        .values(consecutive_failures=0)
    )


async def trip_breaker(db: AsyncSession, endpoint_id: uuid.UUID) -> bool:
    """Guarded one-shot auto-disable: True for exactly one concurrent caller.

    The ``AND enabled`` predicate is the exactly-once mechanism — the caller
    that sees True owns the single WARN log + metric increment (spec §5.5).
    """
    result = await db.execute(
        update(WebhookEndpoint)
        .where(WebhookEndpoint.id == endpoint_id, WebhookEndpoint.enabled.is_(True))
        .values(enabled=False, disabled_reason="circuit_breaker")
        .returning(WebhookEndpoint.id)
    )
    return result.scalar_one_or_none() is not None
