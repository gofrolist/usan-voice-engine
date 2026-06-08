import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import require_admin_role, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.db.models import Elder
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.admin import AssignProfileRequest, ElderSummary

router = APIRouter(
    prefix="/v1/admin/elders",
    tags=["admin-elders"],
    dependencies=[Depends(require_admin_session)],
)


def _mask(phone: str | None) -> str:
    return "***" + phone[-4:] if phone else "unknown"


def _summary(elder: Elder, profile_name: str | None) -> ElderSummary:
    return ElderSummary(
        id=elder.id,
        name=elder.name,
        masked_phone=_mask(elder.phone_e164),
        agent_profile_id=elder.agent_profile_id,
        agent_profile_name=profile_name,
    )


@router.get("", response_model=list[ElderSummary])
async def list_elders(
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[ElderSummary]:
    # Paged: the roster can be large, so never select the whole table at once.
    rows = await elders_repo.list_with_profile(db, limit=limit, offset=offset)
    return [_summary(e, name) for e, name in rows]


@router.put("/{elder_id}/profile", response_model=ElderSummary)
async def assign_profile(
    elder_id: uuid.UUID,
    body: AssignProfileRequest,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ElderSummary:
    try:
        elder = await elders_repo.assign_profile(db, elder_id, body.agent_profile_id)
        if elder is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="elder not found")
        await admin_audit.record(
            db,
            actor_email=actor,
            action="elder.assign_profile",
            entity_type="elder",
            entity_id=str(elder_id),
            detail={
                "agent_profile_id": str(body.agent_profile_id) if body.agent_profile_id else None
            },
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail="unknown agent_profile_id") from exc
    profile_name = None
    if elder.agent_profile_id is not None:
        prof = await profiles_repo.get_profile(db, elder.agent_profile_id)
        profile_name = prof.name if prof else None
    return _summary(elder, profile_name)
