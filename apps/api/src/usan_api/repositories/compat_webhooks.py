"""Compat (RetellAI) webhook subscriptions + delivery outbox repository (feature 003 / US2).

The compat-table twin of ``webhook_endpoints`` + ``webhook_outbox`` (kept SEPARATE so the
native poller never claims/signs a compat row). Mirrors the native worker discipline
verbatim: the claim is an attempt-bump LEASE (no row locks held across POSTs), every
mutator is status-guarded for at-least-once idempotency, ``last_error`` is a TYPE NAME.

``register_subscription`` is the US2 registration seam (US3's create-agent calls it): it
validates the webhook_url against the SSRF gate AND the ``COMPAT_WEBHOOK_ALLOWED_HOSTS``
allow-list (empty list => registration rejected: no PHI webhook can leave), generates a
dedicated signing secret, and upserts one subscription per (org, agent_profile).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import ssrf_guard, webhook_signing
from usan_api.compat.errors import CompatError
from usan_api.db.models import CompatWebhookDelivery, CompatWebhookEndpoint
from usan_api.settings import Settings

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult


# --- Registration seam (T029) ---------------------------------------------------------


async def register_subscription(
    db: AsyncSession,
    settings: Settings,
    *,
    agent_profile_id: uuid.UUID,
    webhook_url: str,
    webhook_events: list[str],
) -> tuple[CompatWebhookEndpoint, str]:
    """Upsert one agent's webhook subscription; return (row, plaintext_secret).

    Validates ``webhook_url`` at registration (FR-022): the SSRF layer-1 gate
    (``validate_webhook_url``) PLUS the compat allow-list — the host must be in
    ``COMPAT_WEBHOOK_ALLOWED_HOSTS``. An empty allow-list rejects ALL registrations
    (fail-closed: no compat webhook ever carries PHI off-box). Rotates the signing secret
    on re-register and re-enables the row (clears a breaker/operator disable).
    """
    try:
        ssrf_guard.validate_webhook_url(webhook_url)
    except ValueError as exc:
        raise CompatError(422, f"invalid webhook_url: {exc}") from exc
    host = (urlsplit(webhook_url).hostname or "").lower().rstrip(".")
    if host not in settings.compat_webhook_allowed_hosts_set:
        # Empty set OR off-list host: PHI-bearing webhooks are disabled for this host.
        raise CompatError(403, "webhook_url host is not in COMPAT_WEBHOOK_ALLOWED_HOSTS")

    secret = webhook_signing.generate_secret()
    stmt = (
        pg_insert(CompatWebhookEndpoint)
        .values(
            agent_profile_id=agent_profile_id,
            webhook_url=webhook_url,
            webhook_events=webhook_events,
            secret=secret,
        )
        .on_conflict_do_update(
            constraint="uq_compat_webhook_agent",
            set_={
                "webhook_url": webhook_url,
                "webhook_events": webhook_events,
                "secret": secret,
                "enabled": True,
                "disabled_reason": None,
                "consecutive_failures": 0,
                "updated_at": func.now(),
            },
        )
        .returning(CompatWebhookEndpoint.id)
    )
    endpoint_id = (await db.execute(stmt)).scalar_one()
    await db.flush()
    row = await db.get(CompatWebhookEndpoint, endpoint_id)
    assert row is not None  # just upserted under this session's org context
    return row, secret


async def get_subscription_for_agent(
    db: AsyncSession, *, agent_profile_id: uuid.UUID, event: str
) -> CompatWebhookEndpoint | None:
    """The enabled subscription for ``agent_profile_id`` that includes ``event`` (RLS-scoped
    to the current org). At most one row per agent per org (uq_compat_webhook_agent)."""
    result = await db.execute(
        select(CompatWebhookEndpoint).where(
            CompatWebhookEndpoint.agent_profile_id == agent_profile_id,
            CompatWebhookEndpoint.enabled.is_(True),
            CompatWebhookEndpoint.webhook_events.contains([event]),
        )
    )
    return result.scalar_one_or_none()


async def enqueue_call_event(
    db: AsyncSession, *, endpoint_id: uuid.UUID, event: str, call_id: uuid.UUID
) -> CompatWebhookDelivery:
    """Insert ONE pending delivery referencing the call (flush-only; joins the caller's txn).

    Payload is MINIMAL — the full ``{event, call}`` body is assembled from the live Call by
    the compat poller at delivery time (off the hot transition path, latest state)."""
    row = CompatWebhookDelivery(
        endpoint_id=endpoint_id,
        event=event,
        payload={"event": event, "call_id": str(call_id)},
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


# --- Worker half (mirrors webhook_outbox.py) ------------------------------------------


@dataclass(frozen=True, slots=True)
class ClaimedCompatDelivery:
    id: uuid.UUID
    endpoint_id: uuid.UUID
    event: str
    payload: dict[str, Any]
    attempts: int


_CLAIM_SQL = text(
    """
    WITH due AS (
        SELECT d.id, d.next_attempt_at
        FROM compat_webhook_deliveries d
        JOIN compat_webhook_endpoints e ON e.id = d.endpoint_id AND e.enabled
        WHERE d.status = 'pending'
          AND d.next_attempt_at <= :now
          AND d.attempts < 4
        ORDER BY d.next_attempt_at
        LIMIT :limit
        FOR UPDATE OF d SKIP LOCKED
    )
    UPDATE compat_webhook_deliveries w
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


async def claim_due(
    db: AsyncSession, *, now: datetime, limit: int = 20
) -> list[ClaimedCompatDelivery]:
    """Claim up to ``limit`` due rows, bumping each onto its next retry rung (crash-safe
    lease; caller commits immediately after). Oldest-due-first."""
    result = await db.execute(_CLAIM_SQL, {"now": now, "limit": limit})
    rows = sorted(result.mappings().all(), key=lambda row: (row["due_at"], row["id"]))
    return [
        ClaimedCompatDelivery(
            id=row["id"],
            endpoint_id=row["endpoint_id"],
            event=row["event"],
            payload=row["payload"],
            attempts=row["attempts"],
        )
        for row in rows
    ]


async def mark_delivered(db: AsyncSession, delivery_id: uuid.UUID, *, response_code: int) -> bool:
    """Guarded success: only a pending row can be delivered (at-least-once idempotency)."""
    result = await db.execute(
        update(CompatWebhookDelivery)
        .where(CompatWebhookDelivery.id == delivery_id, CompatWebhookDelivery.status == "pending")
        .values(status="delivered", delivered_at=func.now(), response_code=response_code)
        .returning(CompatWebhookDelivery.id)
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
    """Guarded failure: record response/last_error; flip to ``failed`` only on the terminal
    (4th) attempt. ``last_error`` is the exception TYPE NAME only (PHI-adjacent rule)."""
    values: dict[str, Any] = {"response_code": response_code, "last_error": last_error}
    if terminal:
        values["status"] = "failed"
    result = await db.execute(
        update(CompatWebhookDelivery)
        .where(CompatWebhookDelivery.id == delivery_id, CompatWebhookDelivery.status == "pending")
        .values(**values)
        .returning(CompatWebhookDelivery.id)
    )
    return result.scalar_one_or_none() is not None


_SWEEP_SQL = text(
    """
    UPDATE compat_webhook_deliveries
    SET status = 'failed', last_error = COALESCE(last_error, 'crash_residual')
    WHERE status = 'pending' AND attempts >= 4
      AND updated_at < CAST(:now AS timestamptz) - interval '10 minutes'
    """
)
_EXPIRE_SQL = text(
    """
    UPDATE compat_webhook_deliveries
    SET status = 'failed', last_error = 'expired'
    WHERE status = 'pending' AND created_at < CAST(:now AS timestamptz) - interval '7 days'
    """
)
_PRUNE_SQL = text(
    """
    DELETE FROM compat_webhook_deliveries
    WHERE status IN ('delivered', 'failed')
      AND created_at < CAST(:now AS timestamptz) - interval '30 days'
    """
)


async def sweep_crash_residue(db: AsyncSession, *, now: datetime) -> int:
    """Fail unclaimable attempts=4 pending rows (crash residue / terminal-rung skips)."""
    result = cast("CursorResult[Any]", await db.execute(_SWEEP_SQL, {"now": now}))
    return int(result.rowcount or 0)


async def expire_stale_pending(db: AsyncSession, *, now: datetime) -> int:
    """Fail pending rows older than 7 days (disabled-endpoint / flag-off backlog bound)."""
    result = cast("CursorResult[Any]", await db.execute(_EXPIRE_SQL, {"now": now}))
    return int(result.rowcount or 0)


async def prune_old(db: AsyncSession, *, now: datetime) -> int:
    """Delete settled rows older than 30 days (hygiene)."""
    result = cast("CursorResult[Any]", await db.execute(_PRUNE_SQL, {"now": now}))
    return int(result.rowcount or 0)


async def count_pending(db: AsyncSession) -> int:
    """Total pending backlog — the per-cycle depth the poller reports."""
    result = await db.execute(
        select(func.count())
        .select_from(CompatWebhookDelivery)
        .where(CompatWebhookDelivery.status == "pending")
    )
    return int(result.scalar_one())


# --- Per-endpoint circuit breaker (mirrors webhook_endpoints.py) -----------------------


async def increment_failures(db: AsyncSession, endpoint_id: uuid.UUID) -> int | None:
    """Atomic +1 on consecutive_failures while enabled; returns the new count or None
    (already disabled — a concurrent trip won)."""
    result = await db.execute(
        update(CompatWebhookEndpoint)
        .where(CompatWebhookEndpoint.id == endpoint_id, CompatWebhookEndpoint.enabled.is_(True))
        .values(consecutive_failures=CompatWebhookEndpoint.consecutive_failures + 1)
        .returning(CompatWebhookEndpoint.consecutive_failures)
    )
    return result.scalar_one_or_none()


async def trip_breaker(db: AsyncSession, endpoint_id: uuid.UUID) -> bool:
    """Guarded one-shot auto-disable: True for exactly one caller (operator re-enable only)."""
    result = await db.execute(
        update(CompatWebhookEndpoint)
        .where(CompatWebhookEndpoint.id == endpoint_id, CompatWebhookEndpoint.enabled.is_(True))
        .values(enabled=False, disabled_reason="circuit_breaker")
        .returning(CompatWebhookEndpoint.id)
    )
    return result.scalar_one_or_none() is not None


async def reset_failures(db: AsyncSession, endpoint_id: uuid.UUID) -> None:
    """Zero the failure counter on a delivered attempt."""
    await db.execute(
        update(CompatWebhookEndpoint)
        .where(CompatWebhookEndpoint.id == endpoint_id)
        .values(consecutive_failures=0)
    )
