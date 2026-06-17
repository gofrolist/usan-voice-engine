import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import SmsMessage


def _utcnow() -> datetime:
    return datetime.now(UTC)


# Defensive cap for list_messages, mirroring the sibling repos (admin_audit /
# follow_up_flags / callback_requests all use _MAX_LIST_LIMIT=500); newest-first
# ordering means the cap keeps the most recent rows.
_MAX_LIST_LIMIT = 500


async def create_sms_message(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    contact_id: uuid.UUID,
    to_number: str,
    template_key: str,
    body: str,
) -> SmsMessage:
    row = SmsMessage(
        call_id=call_id,
        contact_id=contact_id,
        to_number=to_number,
        template_key=template_key,
        body=body,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def create_notification(
    db: AsyncSession,
    *,
    contact_id: uuid.UUID,
    to_number: str,
    kind: str,
    body: str,
    dedupe_key: str | None = None,
) -> SmsMessage | None:
    """Create a non-call notification row (call_id IS NULL) flushed by the outbox.

    Idempotent on ``dedupe_key``: a collision returns the EXISTING row instead of
    inserting a duplicate (ON CONFLICT DO NOTHING on the unique dedupe_key index). With
    no dedupe_key, a plain insert. Flush-only; the caller commits. The unique index is
    non-partial — NULLs are distinct in Postgres — so in-call rows (dedupe_key NULL)
    never collide here.

    The unique is per-org (``UNIQUE(dedupe_key, organization_id)`` — migration 0034), so
    the ON CONFLICT target names both columns; ``organization_id`` is filled by the column
    server-default (the request's RLS org) before the conflict check, and the
    collision-fallback SELECT runs under that same RLS context, so it stays org-scoped.
    """
    if dedupe_key is None:
        row = SmsMessage(
            call_id=None, contact_id=contact_id, to_number=to_number, kind=kind, body=body
        )
        db.add(row)
        await db.flush()
        await db.refresh(row)
        return row
    stmt = (
        pg_insert(SmsMessage)
        .values(
            contact_id=contact_id, to_number=to_number, kind=kind, body=body, dedupe_key=dedupe_key
        )
        .on_conflict_do_nothing(index_elements=[SmsMessage.dedupe_key, SmsMessage.organization_id])
        .returning(SmsMessage.id)
    )
    result = await db.execute(stmt)
    new_id = result.scalar_one_or_none()
    if new_id is None:
        # Dedupe collision: the alert is already enqueued — return the existing row.
        existing = await db.execute(select(SmsMessage).where(SmsMessage.dedupe_key == dedupe_key))
        return existing.scalar_one_or_none()
    await db.flush()
    return await db.get(SmsMessage, new_id)


async def get_pending_notifications(db: AsyncSession, *, limit: int = 50) -> list[SmsMessage]:
    """Pending notification rows (no owning call) for the notification outbox poller.

    Served by idx_sms_notifications (partial: WHERE call_id IS NULL). In-call rows are
    flushed by sms_outbox and are deliberately NOT returned here.
    """
    limit = max(1, min(limit, _MAX_LIST_LIMIT))
    result = await db.execute(
        select(SmsMessage)
        .where(SmsMessage.call_id.is_(None), SmsMessage.status == "pending")
        .order_by(SmsMessage.created_at)
        .limit(limit)
    )
    return list(result.scalars().all())


async def claim_pending_notification(db: AsyncSession) -> SmsMessage | None:
    """Lock + return the oldest claimable pending notification (FOR UPDATE SKIP LOCKED).

    The row lock is held until the caller's transaction commits, so a second concurrent
    poller (another replica) SKIPs this row rather than re-sending it. The caller sends,
    then marks the row sent/failed/suppressed, then commits — releasing the lock. SKIP
    LOCKED means concurrent pollers make progress on disjoint rows instead of blocking.
    """
    result = await db.execute(
        select(SmsMessage)
        .where(SmsMessage.call_id.is_(None), SmsMessage.status == "pending")
        .order_by(SmsMessage.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    return result.scalar_one_or_none()


async def count_for_call(db: AsyncSession, call_id: uuid.UUID) -> int:
    """All sms rows for a call, regardless of status (the per-call send budget)."""
    result = await db.execute(
        select(func.count()).select_from(SmsMessage).where(SmsMessage.call_id == call_id)
    )
    return int(result.scalar_one())


async def get_pending_for_call(db: AsyncSession, call_id: uuid.UUID) -> list[SmsMessage]:
    result = await db.execute(
        select(SmsMessage)
        .where(SmsMessage.call_id == call_id, SmsMessage.status == "pending")
        .order_by(SmsMessage.created_at)
    )
    return list(result.scalars().all())


async def mark_sent(
    db: AsyncSession, sms_id: uuid.UUID, *, telnyx_message_id: str
) -> SmsMessage | None:
    """Status-guarded pending->sent. Returns the row, or None if it was not pending
    (idempotent: a second flush claims nothing)."""
    now = _utcnow()
    result = await db.execute(
        update(SmsMessage)
        .where(SmsMessage.id == sms_id, SmsMessage.status == "pending")
        .values(
            status="sent",
            telnyx_message_id=telnyx_message_id,
            sent_at=now,
            updated_at=now,
        )
        .returning(SmsMessage.id)
    )
    if result.scalar_one_or_none() is None:
        return None
    await db.flush()
    row = await db.get(SmsMessage, sms_id)
    if row is None:
        # The guard above claimed the row, but a concurrent transaction deleted it
        # before this GET. Treat it as "nothing to return" rather than crashing in
        # db.refresh(None).
        return None
    await db.refresh(row)
    return row


async def mark_failed(
    db: AsyncSession, sms_id: uuid.UUID, *, error: dict[str, Any]
) -> SmsMessage | None:
    """Status-guarded pending->failed. Returns the row, or None if not pending."""
    result = await db.execute(
        update(SmsMessage)
        .where(SmsMessage.id == sms_id, SmsMessage.status == "pending")
        .values(status="failed", error=error, updated_at=_utcnow())
        .returning(SmsMessage.id)
    )
    if result.scalar_one_or_none() is None:
        return None
    await db.flush()
    row = await db.get(SmsMessage, sms_id)
    if row is None:
        # Concurrent delete between the guarded UPDATE and this GET — no row to refresh.
        return None
    await db.refresh(row)
    return row


async def mark_suppressed(db: AsyncSession, sms_id: uuid.UUID, *, reason: str) -> SmsMessage | None:
    """Status-guarded pending->suppressed: a TERMINAL, intentional non-send (recipient
    opted out / on the DNC list). Distinct from 'failed' (a delivery error that inflates
    failure metrics and implies retry). Returns the row, or None if it was not pending."""
    result = await db.execute(
        update(SmsMessage)
        .where(SmsMessage.id == sms_id, SmsMessage.status == "pending")
        .values(status="suppressed", error={"reason": reason}, updated_at=_utcnow())
        .returning(SmsMessage.id)
    )
    if result.scalar_one_or_none() is None:
        return None
    await db.flush()
    row = await db.get(SmsMessage, sms_id)
    if row is None:
        return None
    await db.refresh(row)
    return row


async def list_messages(
    db: AsyncSession, *, status: str | None = None, limit: int = 100
) -> list[SmsMessage]:
    limit = max(1, min(limit, _MAX_LIST_LIMIT))
    stmt = select(SmsMessage)
    if status is not None:
        stmt = stmt.where(SmsMessage.status == status)
    stmt = stmt.order_by(SmsMessage.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())
