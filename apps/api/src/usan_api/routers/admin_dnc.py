from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import get_tenant_db, require_admin_role, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.repositories import admin_audit
from usan_api.repositories import dnc as dnc_repo
from usan_api.schemas._validators import E164_PATTERN, PHONE_MAX_LENGTH
from usan_api.schemas.dnc import AdminDNCResponse, DNCCreate

router = APIRouter(
    prefix="/v1/admin/dnc",
    tags=["admin-dnc"],
    dependencies=[Depends(require_admin_session)],
)


@router.get("", response_model=list[AdminDNCResponse])
async def list_dnc(
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_tenant_db),
) -> list[AdminDNCResponse]:
    rows = await dnc_repo.list_entries(db, limit=limit, offset=offset)
    return [AdminDNCResponse.from_model(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=AdminDNCResponse)
async def add_dnc(
    body: DNCCreate,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> AdminDNCResponse:
    await dnc_repo.lock_phone(db, body.phone_e164)
    entry = await dnc_repo.add_entry(db, body.phone_e164, body.reason)
    await admin_audit.record(
        db,
        actor_email=actor,
        action="dnc.add",
        entity_type="dnc",
        entity_id=None,  # phone IS the PK and is PHI — never put it in entity_id/detail
        detail={"has_reason": body.reason is not None},
    )
    await db.commit()
    return AdminDNCResponse.from_model(entry)


@router.delete("/{phone_e164}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_dnc(
    phone_e164: str = Path(min_length=1, max_length=PHONE_MAX_LENGTH, pattern=E164_PATTERN),
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    removed = await dnc_repo.remove_entry(db, phone_e164)
    if not removed:
        raise HTTPException(status_code=404, detail="not on DNC list")
    await admin_audit.record(
        db, actor_email=actor, action="dnc.remove", entity_type="dnc", entity_id=None
    )
    await db.commit()
