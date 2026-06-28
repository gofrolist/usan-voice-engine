"""chat_analyses repository (Phase 4c-2). Post-chat analysis; RLS-scoped, org auto-filled.

``upsert`` overwrites in place (the rerun op recomputes), keyed on the unique
``chat_session_id``. ``get_for_sessions`` batches the list path (one IN query → no N+1).
Flush-only; the caller commits. Mirrors ``conversation_summaries`` for the chat channel.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import ChatAnalysisRecord


async def get_for_session(db: AsyncSession, session_id: uuid.UUID) -> ChatAnalysisRecord | None:
    stmt = select(ChatAnalysisRecord).where(ChatAnalysisRecord.chat_session_id == session_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def get_for_sessions(
    db: AsyncSession, session_ids: list[uuid.UUID]
) -> dict[uuid.UUID, ChatAnalysisRecord]:
    if not session_ids:
        return {}
    stmt = select(ChatAnalysisRecord).where(ChatAnalysisRecord.chat_session_id.in_(session_ids))
    rows = (await db.execute(stmt)).scalars().all()
    return {r.chat_session_id: r for r in rows}


async def upsert(
    db: AsyncSession,
    session_id: uuid.UUID,
    *,
    chat_summary: str | None,
    user_sentiment: str | None,
    chat_successful: bool | None,
    custom_analysis_data: dict[str, Any] | None,
    model_version: str,
) -> ChatAnalysisRecord:
    """Insert or overwrite the analysis for ``session_id`` (the rerun op recomputes).

    ON CONFLICT (chat_session_id) DO UPDATE keeps one row per chat. organization_id is
    never set here — the DB default fills it and RLS WITH CHECK enforces the tenant.
    """
    values = {
        "chat_summary": chat_summary,
        "user_sentiment": user_sentiment,
        "chat_successful": chat_successful,
        "custom_analysis_data": custom_analysis_data,
        "model_version": model_version,
    }
    stmt = (
        pg_insert(ChatAnalysisRecord)
        .values(chat_session_id=session_id, **values)
        .on_conflict_do_update(
            index_elements=[ChatAnalysisRecord.chat_session_id],
            set_={**values, "updated_at": func.now()},
        )
    )
    await db.execute(stmt)
    await db.flush()
    # Re-select with populate_existing so the identity-map row (if a caller already loaded
    # it) is refreshed to the just-written values — a surgical refresh, NOT a session-wide
    # expire_all (which would also expire the caller's other loaded objects, e.g. ChatSession).
    result = await db.execute(
        select(ChatAnalysisRecord)
        .where(ChatAnalysisRecord.chat_session_id == session_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()
