from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import require_admin_role, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.db.models import AdminUser
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit
from usan_api.repositories import admin_users as repo
from usan_api.schemas.auth import AdminUserCreate, AdminUserOut

router = APIRouter(
    prefix="/v1/admin/admin-users",
    tags=["admin-users"],
    dependencies=[Depends(require_admin_session)],
)


def _to_out(user: AdminUser) -> AdminUserOut:
    return AdminUserOut(email=user.email, role=user.role.value, added_by=user.added_by)


@router.get("", response_model=list[AdminUserOut])
async def list_users(db: AsyncSession = Depends(get_db)) -> list[AdminUserOut]:
    return [_to_out(u) for u in await repo.list_admin_users(db)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=AdminUserOut)
async def add_user(
    body: AdminUserCreate,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> AdminUserOut:
    user = await repo.add_admin_user(
        db, email=body.email, role=AdminRole(body.role), added_by=actor
    )
    await admin_audit.record(
        db,
        actor_email=actor,
        action="admin_user.add",
        entity_type="admin_user",
        entity_id=user.email,
        detail={"role": body.role},
    )
    await db.commit()
    return _to_out(user)


@router.delete("/{email}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_user(
    email: str,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    removed = await repo.remove_admin_user(db, email)
    if not removed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="admin user not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="admin_user.remove",
        entity_type="admin_user",
        entity_id=email.lower(),
    )
    await db.commit()
