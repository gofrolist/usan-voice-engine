"""Per-org invitation management (P3). Active org comes from the session principal
(get_tenant_db), never the URL. invitations is a GLOBAL (non-RLS) table, so every
query is scoped by principal.active_org_id in app code (see repositories/invitations).
All endpoints require ADMIN — invite links are not shown to viewers."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import (
    AdminPrincipal,
    get_tenant_db,
    require_admin_role,
    require_admin_session,
)
from usan_api.db.base import AdminRole
from usan_api.db.models import Invitation
from usan_api.invites import build_accept_url
from usan_api.repositories import admin_audit
from usan_api.repositories import invitations as repo
from usan_api.schemas.invites import InviteCreate, InviteOut
from usan_api.settings import Settings, get_settings

router = APIRouter(
    prefix="/v1/admin/invites",
    tags=["invites"],
    dependencies=[Depends(require_admin_session)],
)


def _out(inv: Invitation, settings: Settings) -> InviteOut:
    return InviteOut(
        id=inv.id,
        email=inv.email,
        role=inv.role.value,
        status=inv.status.value,
        accept_url=build_accept_url(settings, inv.token),
        expires_at=inv.expires_at,
        created_at=inv.created_at,
        invited_by=inv.invited_by,
    )


@router.get("", response_model=list[InviteOut])
async def list_invites(
    principal: AdminPrincipal = Depends(require_admin_session),
    db: AsyncSession = Depends(get_tenant_db),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
    settings: Settings = Depends(get_settings),
) -> list[InviteOut]:
    assert principal.active_org_id is not None
    return [_out(i, settings) for i in await repo.list_pending(db, principal.active_org_id)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InviteOut)
async def create_invite(
    body: InviteCreate,
    principal: AdminPrincipal = Depends(require_admin_session),
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
    settings: Settings = Depends(get_settings),
) -> InviteOut:
    assert principal.active_org_id is not None
    try:
        inv = await repo.create_invite(
            db,
            org_id=principal.active_org_id,
            email=body.email,
            role=AdminRole(body.role),
            invited_by=actor,
            ttl_hours=settings.invite_ttl_hours,
        )
    except repo.AlreadyMemberError as e:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "already a member of this organization"
        ) from e
    except IntegrityError as e:
        # A concurrent create for the same (org, email) can race past create_invite's
        # existing-pending SELECT and trip the partial unique index on INSERT. Surface the
        # same clean 409 the rest of the codebase returns, not an opaque 500.
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "a pending invite for this email already exists"
        ) from e
    await admin_audit.record(
        db,
        actor_email=actor,
        action="invite.create",
        entity_type="invitation",
        entity_id=inv.email,
        detail={"role": body.role},
    )
    await db.commit()
    return _out(inv, settings)


@router.delete("/{invite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invite(
    invite_id: uuid.UUID,
    principal: AdminPrincipal = Depends(require_admin_session),
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    assert principal.active_org_id is not None
    inv = await repo.get_invite(db, invite_id, principal.active_org_id)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite not found")
    try:
        await repo.revoke(db, inv)
    except repo.NotPendingError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "invite is not pending") from e
    await admin_audit.record(
        db,
        actor_email=actor,
        action="invite.revoke",
        entity_type="invitation",
        entity_id=inv.email,
    )
    await db.commit()


@router.post("/{invite_id}/resend", response_model=InviteOut)
async def resend_invite(
    invite_id: uuid.UUID,
    principal: AdminPrincipal = Depends(require_admin_session),
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
    settings: Settings = Depends(get_settings),
) -> InviteOut:
    assert principal.active_org_id is not None
    inv = await repo.get_invite(db, invite_id, principal.active_org_id)
    if inv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "invite not found")
    try:
        inv = await repo.resend(db, inv, ttl_hours=settings.invite_ttl_hours)
    except repo.NotPendingError as e:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "invite is not pending") from e
    await admin_audit.record(
        db,
        actor_email=actor,
        action="invite.resend",
        entity_type="invitation",
        entity_id=inv.email,
    )
    await db.commit()
    return _out(inv, settings)
