from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import ConversationFlow


async def create(db: AsyncSession, *, config: dict[str, Any], version: int = 0) -> ConversationFlow:
    cf = ConversationFlow(config=config, version=version)
    db.add(cf)
    await db.flush()
    await db.refresh(cf)
    return cf


async def get(db: AsyncSession, flow_id: uuid.UUID) -> ConversationFlow | None:
    result = await db.execute(
        select(ConversationFlow).where(
            ConversationFlow.id == flow_id, ConversationFlow.archived_at.is_(None)
        )
    )
    return result.scalar_one_or_none()


async def update(
    db: AsyncSession, flow_id: uuid.UUID, *, config: dict[str, Any], version: int
) -> ConversationFlow | None:
    cf = await get(db, flow_id)
    if cf is None:
        return None
    cf.config = config
    cf.version = version
    await db.flush()
    await db.refresh(cf)
    return cf


async def archive(db: AsyncSession, flow_id: uuid.UUID) -> bool:
    cf = await get(db, flow_id)
    if cf is None:
        return False
    cf.archived_at = datetime.now(UTC)
    await db.flush()
    return True


async def list_flows(
    db: AsyncSession,
    *,
    limit: int,
    descending: bool,
    after: tuple[datetime, uuid.UUID] | None,
) -> list[ConversationFlow]:
    """Keyset-paginate the org's non-archived flows over (created_at, id). RLS scopes to the
    caller's org. Fetches limit+1 so the caller computes has_more without a COUNT."""
    stmt = select(ConversationFlow).where(ConversationFlow.archived_at.is_(None))
    if after is not None:
        after_created_at, after_id = after
        if descending:
            stmt = stmt.where(
                or_(
                    ConversationFlow.created_at < after_created_at,
                    and_(
                        ConversationFlow.created_at == after_created_at,
                        ConversationFlow.id < after_id,
                    ),
                )
            )
        else:
            stmt = stmt.where(
                or_(
                    ConversationFlow.created_at > after_created_at,
                    and_(
                        ConversationFlow.created_at == after_created_at,
                        ConversationFlow.id > after_id,
                    ),
                )
            )
    if descending:
        stmt = stmt.order_by(ConversationFlow.created_at.desc(), ConversationFlow.id.desc())
    else:
        stmt = stmt.order_by(ConversationFlow.created_at.asc(), ConversationFlow.id.asc())
    stmt = stmt.limit(limit + 1)
    return list((await db.execute(stmt)).scalars().all())
