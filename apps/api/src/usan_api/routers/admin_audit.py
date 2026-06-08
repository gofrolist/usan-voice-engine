from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import require_admin_session
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit as repo
from usan_api.schemas.admin import AuditEntryOut

router = APIRouter(
    prefix="/v1/admin/audit",
    tags=["admin-audit"],
    dependencies=[Depends(require_admin_session)],
)


@router.get("", response_model=list[AuditEntryOut])
async def list_audit(
    limit: int = Query(default=100, ge=1, le=500),
    # Server-side filters so a match spans the whole table, not just the latest
    # `limit` rows (a compliance screen must not show a false "no matching entries").
    actor: str | None = Query(default=None, max_length=320),
    action: str | None = Query(default=None, max_length=100),
    db: AsyncSession = Depends(get_db),
) -> list[AuditEntryOut]:
    rows = await repo.list_recent(db, limit=limit, actor=actor, action=action)
    return [
        AuditEntryOut(
            id=r.id,
            actor_email=r.actor_email,
            action=r.action,
            entity_type=r.entity_type,
            entity_id=r.entity_id,
            detail=r.detail,
            created_at=r.created_at,
        )
        for r in rows
    ]
