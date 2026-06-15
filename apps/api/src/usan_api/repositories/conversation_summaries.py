"""conversation_summaries repository (US4 / T046).

A per-call carry-forward recap. ``create`` is idempotent on ``call_id`` (the unique
index), so a re-fired completion trigger inserts nothing new. ``get_latest`` feeds the
``last_call_summary`` / ``open_plans`` built-ins. Flush-only; the caller commits.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import ConversationSummary


async def get_for_call(db: AsyncSession, call_id: uuid.UUID) -> ConversationSummary | None:
    """The summary for a specific call, if one was already written (idempotency check)."""
    stmt = select(ConversationSummary).where(ConversationSummary.call_id == call_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_latest(db: AsyncSession, *, contact_id: uuid.UUID) -> ConversationSummary | None:
    """The contact's most recent summary (newest created_at, then id) — or None."""
    stmt = (
        select(ConversationSummary)
        .where(ConversationSummary.contact_id == contact_id)
        .order_by(ConversationSummary.created_at.desc(), ConversationSummary.id.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def create(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    contact_id: uuid.UUID,
    summary: str,
    open_plans: list[Any],
    model_version: str,
) -> ConversationSummary | None:
    """Insert one summary; returns None if a summary for this call already exists.

    ON CONFLICT DO NOTHING on the unique ``call_id`` keeps a re-fired completion
    trigger from creating a duplicate recap (one summary per call). Flush-only.
    """
    stmt = (
        pg_insert(ConversationSummary)
        .values(
            call_id=call_id,
            contact_id=contact_id,
            summary=summary,
            open_plans=open_plans,
            model_version=model_version,
        )
        .on_conflict_do_nothing(index_elements=[ConversationSummary.call_id])
        .returning(ConversationSummary.id)
    )
    new_id = (await db.execute(stmt)).scalar_one_or_none()
    if new_id is None:
        return None
    await db.flush()
    return await get_for_call(db, call_id)
