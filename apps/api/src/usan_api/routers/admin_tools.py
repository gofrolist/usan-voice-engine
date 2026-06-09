"""Admin read endpoints for the Phase-3 tool tables (design §5/§6).

Session-gated (require_admin_session). Reads that expose PHI (`follow_up_flags`)
record a PHI-FREE audit entry: only the actor + filter shape, never `reason`.
C and D ADD their summary route to this file (additive; no re-register).
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import require_admin_session
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit
from usan_api.repositories import follow_up_flags as follow_up_flags_repo
from usan_api.schemas.admin_tools import FollowupFlagSummary

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin-tools"],
    dependencies=[Depends(require_admin_session)],
)


@router.get("/follow-up-flags", response_model=list[FollowupFlagSummary])
async def list_follow_up_flags(
    status: str | None = Query(default=None, max_length=32),
    elder_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
) -> list[FollowupFlagSummary]:
    rows = await follow_up_flags_repo.list_flags(db, status=status, elder_id=elder_id, limit=limit)
    # PHI read (reason) -> audit. Detail carries only the filter shape + count,
    # NEVER the reason text or an elder's name/phone (PHI-free; spec §9).
    await admin_audit.record(
        db,
        actor_email=actor,
        action="follow_up_flags.list",
        entity_type="follow_up_flag",
        entity_id=str(elder_id) if elder_id is not None else None,
        detail={"status": status, "count": len(rows)},
    )
    await db.commit()
    return [FollowupFlagSummary.model_validate(r) for r in rows]
