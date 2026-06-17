"""Per-org membership management (P2). The active org is resolved from the session
principal (get_tenant_db), never the URL — an admin manages only the members of the
org they are currently in. memberships is a GLOBAL (non-RLS) table, so every query
is scoped by ``principal.active_org_id`` in app code (see repositories/memberships).
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import (
    AdminPrincipal,
    get_tenant_db,
    require_admin_role,
    require_admin_session,
)
from usan_api.db.base import AdminRole
from usan_api.db.models import Membership
from usan_api.repositories import admin_audit
from usan_api.repositories import memberships as repo
from usan_api.schemas.members import MemberCreate, MemberOut, MemberRoleUpdate

router = APIRouter(
    prefix="/v1/admin/members",
    tags=["members"],
    dependencies=[Depends(require_admin_session)],
)


def _out(m: Membership) -> MemberOut:
    return MemberOut(email=m.email, role=m.role.value, added_by=m.added_by)


@router.get("", response_model=list[MemberOut])
async def list_members(
    principal: AdminPrincipal = Depends(require_admin_session),
    db: AsyncSession = Depends(get_tenant_db),
) -> list[MemberOut]:
    assert principal.active_org_id is not None  # get_tenant_db 409s when None
    return [_out(m) for m in await repo.list_members(db, principal.active_org_id)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=MemberOut)
async def add_member(
    body: MemberCreate,
    principal: AdminPrincipal = Depends(require_admin_session),
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> MemberOut:
    assert principal.active_org_id is not None
    m = await repo.add_member(
        db,
        email=body.email,
        org_id=principal.active_org_id,
        role=AdminRole(body.role),
        added_by=actor,
    )
    await admin_audit.record(
        db,
        actor_email=actor,
        action="member.add",
        entity_type="membership",
        entity_id=m.email,
        detail={"role": body.role},
    )
    await db.commit()
    return _out(m)


@router.patch("/{email}", response_model=MemberOut)
async def set_role(
    email: str,
    body: MemberRoleUpdate,
    principal: AdminPrincipal = Depends(require_admin_session),
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> MemberOut:
    assert principal.active_org_id is not None
    try:
        m = await repo.set_member_role(
            db, email=email, org_id=principal.active_org_id, role=AdminRole(body.role)
        )
    except repo.LastOrgAdminError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except KeyError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found") from e
    await admin_audit.record(
        db,
        actor_email=actor,
        action="member.role",
        entity_type="membership",
        entity_id=email.lower(),
        detail={"role": body.role},
    )
    await db.commit()
    return _out(m)


@router.delete("/{email}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    email: str,
    principal: AdminPrincipal = Depends(require_admin_session),
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    assert principal.active_org_id is not None
    try:
        removed = await repo.remove_member(db, email=email, org_id=principal.active_org_id)
    except repo.LastOrgAdminError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    if not removed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "member not found")
    await admin_audit.record(
        db,
        actor_email=actor,
        action="member.remove",
        entity_type="membership",
        entity_id=email.lower(),
    )
    await db.commit()
