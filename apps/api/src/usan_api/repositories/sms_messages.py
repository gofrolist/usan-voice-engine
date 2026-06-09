import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import SmsMessage


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def create_sms_message(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    elder_id: uuid.UUID,
    to_number: str,
    template_key: str,
    body: str,
) -> SmsMessage:
    row = SmsMessage(
        call_id=call_id,
        elder_id=elder_id,
        to_number=to_number,
        template_key=template_key,
        body=body,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


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
    await db.refresh(row)
    return row


async def list_messages(
    db: AsyncSession, *, status: str | None = None, limit: int = 100
) -> list[SmsMessage]:
    stmt = select(SmsMessage)
    if status is not None:
        stmt = stmt.where(SmsMessage.status == status)
    stmt = stmt.order_by(SmsMessage.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())
