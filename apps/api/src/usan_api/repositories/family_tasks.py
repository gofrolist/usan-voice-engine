"""family_tasks repository (US2 / T026).

State machine (guarded UPDATEs — the WHERE clause IS the state machine, like
follow_up_flags.update_status): open → delivered → closed; open → needs_review →
open/closed. Only ``open`` non-safety-review tasks are injected as the
``open_family_tasks`` builtin. All functions are flush-only; the caller commits.
"""

import uuid

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from usan_api.db.models import FamilyTask

# Bound the per-elder open-task injection so a backlog never floods the prompt.
_MAX_OPEN_INJECT = 20
_MAX_LIST_LIMIT = 500


async def create_family_task(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID,
    family_contact_id: uuid.UUID | None,
    message: str,
    needs_safety_review: bool = False,
    status: str = "open",
) -> FamilyTask:
    row = FamilyTask(
        elder_id=elder_id,
        family_contact_id=family_contact_id,
        message=message,
        needs_safety_review=needs_safety_review,
        status=status,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def create_inbound_task(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID,
    family_contact_id: uuid.UUID | None,
    message: str,
    inbound_message_id: str,
    needs_safety_review: bool = False,
) -> FamilyTask | None:
    """Idempotent insert keyed on the Telnyx inbound message id (FR-008, T029).

    A redelivered webhook with the same message id is a no-op: ON CONFLICT DO NOTHING
    on the unique ``inbound_message_id`` returns the pre-existing row instead of a
    duplicate task. Mirrors ``sms_messages.create_notification``. Flush-only.
    """
    insert_stmt = (
        pg_insert(FamilyTask)
        .values(
            elder_id=elder_id,
            family_contact_id=family_contact_id,
            message=message,
            status="open",
            needs_safety_review=needs_safety_review,
            inbound_message_id=inbound_message_id,
        )
        .on_conflict_do_nothing(index_elements=[FamilyTask.inbound_message_id])
        .returning(FamilyTask.id)
    )
    new_id = (await db.execute(insert_stmt)).scalar_one_or_none()
    if new_id is not None:
        await db.flush()
        return await get_family_task(db, new_id)
    # Conflict: the message was already processed — return the existing task.
    existing = await db.execute(
        select(FamilyTask).where(FamilyTask.inbound_message_id == inbound_message_id)
    )
    return existing.scalar_one_or_none()


async def get_family_task(db: AsyncSession, task_id: int) -> FamilyTask | None:
    stmt = (
        select(FamilyTask).where(FamilyTask.id == task_id).execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_open_family_tasks(
    db: AsyncSession, *, elder_id: uuid.UUID, limit: int = _MAX_OPEN_INJECT
) -> list[FamilyTask]:
    """Open, non-safety-review tasks to convey on the next call (oldest first)."""
    stmt = (
        select(FamilyTask)
        .where(
            FamilyTask.elder_id == elder_id,
            FamilyTask.status == "open",
            FamilyTask.needs_safety_review.is_(False),
        )
        .order_by(FamilyTask.created_at, FamilyTask.id)
        .limit(max(1, limit))
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_family_tasks(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[FamilyTask]:
    """Admin read model: newest-first, optionally filtered by elder/status."""
    limit = max(1, min(limit, _MAX_LIST_LIMIT))
    offset = max(0, offset)
    stmt = select(FamilyTask)
    if elder_id is not None:
        stmt = stmt.where(FamilyTask.elder_id == elder_id)
    if status is not None:
        stmt = stmt.where(FamilyTask.status == status)
    # needs-review tasks first (operators must action them), then newest-first.
    stmt = (
        stmt.order_by(
            FamilyTask.needs_safety_review.desc(),
            FamilyTask.created_at.desc(),
            FamilyTask.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return list((await db.execute(stmt)).scalars().all())


async def mark_delivered(
    db: AsyncSession, task_id: int, *, call_id: uuid.UUID
) -> FamilyTask | None:
    """open → delivered, recording which call conveyed it. Zero rows → None."""
    stmt = (
        update(FamilyTask)
        .where(FamilyTask.id == task_id, FamilyTask.status == "open")
        .values(
            status="delivered",
            delivered_call_id=call_id,
            status_updated_at=func.now(),
            status_updated_by="agent",
        )
        .returning(FamilyTask)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def close_family_task(db: AsyncSession, task_id: int, *, actor: str) -> FamilyTask | None:
    """(open|delivered|needs_review) → closed. Zero rows → None (already closed/unknown)."""
    stmt = (
        update(FamilyTask)
        .where(
            FamilyTask.id == task_id,
            FamilyTask.status.in_(("open", "delivered", "needs_review")),
        )
        .values(status="closed", status_updated_at=func.now(), status_updated_by=actor)
        .returning(FamilyTask)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def mark_all_delivered(
    db: AsyncSession, *, elder_id: uuid.UUID, call_id: uuid.UUID
) -> list[FamilyTask]:
    """Mark every conveyed open task delivered for an elder in one guarded UPDATE (T031).

    Targets exactly the set ``list_open_family_tasks`` injects (``status='open' AND
    needs_safety_review IS FALSE``) — a safety-review task that was never conveyed is
    left untouched. Used by the ``close_family_task`` tool when the LLM omits a
    ``task_id`` (it only ever sees task TEXT, not ids). Idempotent: a re-call after the
    rows are already delivered touches zero rows and returns ``[]``. Flush-only.
    """
    stmt = (
        update(FamilyTask)
        .where(
            FamilyTask.elder_id == elder_id,
            FamilyTask.status == "open",
            FamilyTask.needs_safety_review.is_(False),
        )
        .values(
            status="delivered",
            delivered_call_id=call_id,
            status_updated_at=func.now(),
            status_updated_by="agent",
        )
        .returning(FamilyTask)
    )
    return list((await db.execute(stmt)).scalars().all())


async def approve_family_task(db: AsyncSession, task_id: int, *, actor: str) -> FamilyTask | None:
    """Operator approves a held task (FR-015): clear ``needs_safety_review`` so it injects.

    Guarded — only a not-yet-closed task that is currently ``needs_safety_review`` flips;
    zero rows (already approved / closed / unknown) → None so the caller can 404/409. The
    status itself stays ``open``; the review flag is the gate. Flush-only.
    """
    stmt = (
        update(FamilyTask)
        .where(
            FamilyTask.id == task_id,
            FamilyTask.needs_safety_review.is_(True),
            FamilyTask.status != "closed",
        )
        .values(needs_safety_review=False, status_updated_at=func.now(), status_updated_by=actor)
        .returning(FamilyTask)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def set_status(
    db: AsyncSession,
    task_id: int,
    *,
    new_status: str,
    actor: str,
    allowed_from: tuple[str, ...],
) -> FamilyTask | None:
    """Generic guarded transition for the admin plane (e.g. approve needs_review → open).

    Zero rows updated (status not in ``allowed_from``) → None so the caller can
    distinguish 404 / no-op / conflict.
    """
    stmt = (
        update(FamilyTask)
        .where(FamilyTask.id == task_id, FamilyTask.status.in_(allowed_from))
        .values(status=new_status, status_updated_at=func.now(), status_updated_by=actor)
        .returning(FamilyTask)
    )
    return (await db.execute(stmt)).scalar_one_or_none()
