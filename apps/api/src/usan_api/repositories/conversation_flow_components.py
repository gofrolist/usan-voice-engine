from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import ConversationFlowComponent


async def create(db: AsyncSession, *, config: dict[str, Any]) -> ConversationFlowComponent:
    component = ConversationFlowComponent(config=config)
    db.add(component)
    await db.flush()
    await db.refresh(component)
    return component


async def get(db: AsyncSession, component_id: uuid.UUID) -> ConversationFlowComponent | None:
    result = await db.execute(
        select(ConversationFlowComponent).where(
            ConversationFlowComponent.id == component_id,
            ConversationFlowComponent.archived_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def update(
    db: AsyncSession, component_id: uuid.UUID, *, config: dict[str, Any]
) -> ConversationFlowComponent | None:
    component = await get(db, component_id)
    if component is None:
        return None
    component.config = config
    await db.flush()
    await db.refresh(component)
    return component


async def archive(db: AsyncSession, component_id: uuid.UUID) -> bool:
    component = await get(db, component_id)
    if component is None:
        return False
    component.archived_at = datetime.now(UTC)
    await db.flush()
    return True


async def list_components(
    db: AsyncSession,
    *,
    limit: int,
    descending: bool,
    after: tuple[datetime, uuid.UUID] | None,
) -> list[ConversationFlowComponent]:
    """Keyset-paginate the org's non-archived components over (created_at, id). RLS scopes to the
    caller's org. Fetches limit+1 so the caller computes has_more without a COUNT."""
    stmt = select(ConversationFlowComponent).where(ConversationFlowComponent.archived_at.is_(None))
    if after is not None:
        after_created_at, after_id = after
        if descending:
            stmt = stmt.where(
                or_(
                    ConversationFlowComponent.created_at < after_created_at,
                    and_(
                        ConversationFlowComponent.created_at == after_created_at,
                        ConversationFlowComponent.id < after_id,
                    ),
                )
            )
        else:
            stmt = stmt.where(
                or_(
                    ConversationFlowComponent.created_at > after_created_at,
                    and_(
                        ConversationFlowComponent.created_at == after_created_at,
                        ConversationFlowComponent.id > after_id,
                    ),
                )
            )
    if descending:
        stmt = stmt.order_by(
            ConversationFlowComponent.created_at.desc(), ConversationFlowComponent.id.desc()
        )
    else:
        stmt = stmt.order_by(
            ConversationFlowComponent.created_at.asc(), ConversationFlowComponent.id.asc()
        )
    stmt = stmt.limit(limit + 1)
    return list((await db.execute(stmt)).scalars().all())
