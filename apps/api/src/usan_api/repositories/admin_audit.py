from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import AdminAuditLog

_MAX_LIST_LIMIT = 500


async def record(
    db: AsyncSession,
    *,
    actor_email: str,
    action: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AdminAuditLog:
    """Append an audit entry. Caller owns the surrounding transaction/commit."""
    entry = AdminAuditLog(
        actor_email=actor_email,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        detail=detail or {},
    )
    db.add(entry)
    await db.flush()
    await db.refresh(entry)
    return entry


async def list_recent(db: AsyncSession, *, limit: int = 100) -> list[AdminAuditLog]:
    limit = max(1, min(limit, _MAX_LIST_LIMIT))
    result = await db.execute(
        select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc()).limit(limit)
    )
    return list(result.scalars().all())
