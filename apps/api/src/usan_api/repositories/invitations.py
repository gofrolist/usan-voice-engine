"""Invitation data access (P3). The invitations table is GLOBAL (non-RLS), so the
management functions scope by organization_id in app code; accept looks up globally
by the unique token. created_at/id come from DB defaults (read back via refresh)."""

import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import AdminRole, InviteStatus
from usan_api.db.models import Invitation
from usan_api.repositories import memberships as memberships_repo


class AlreadyMemberError(Exception):
    """Refuse inviting someone who is already a member of the org."""


class NotPendingError(Exception):
    """Revoke/resend attempted on an invite that is no longer pending."""


def _norm(email: str) -> str:
    return email.strip().lower()


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def is_usable(invite: Invitation, *, now: datetime) -> bool:
    """An invite can be accepted iff it is pending and unexpired (lazy expiry)."""
    return invite.status is InviteStatus.PENDING and invite.expires_at > now


async def create_invite(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    email: str,
    role: AdminRole,
    invited_by: str | None,
    ttl_hours: int,
) -> Invitation:
    """Create (or regenerate the existing pending) invite for (org, email).

    Rejects inviting an existing member. The partial unique index
    (organization_id, email) WHERE status='pending' is the integrity backstop for the
    select-then-insert below.
    """
    norm = _norm(email)
    if await memberships_repo.get_membership(db, norm, org_id) is not None:
        raise AlreadyMemberError("already a member of this organization")
    expires = datetime.now(UTC) + timedelta(hours=ttl_hours)
    existing = (
        await db.execute(
            select(Invitation).where(
                Invitation.organization_id == org_id,
                Invitation.email == norm,
                Invitation.status == InviteStatus.PENDING,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.token = _new_token()
        existing.expires_at = expires
        existing.role = role
        await db.flush()
        return existing
    invite = Invitation(
        organization_id=org_id,
        email=norm,
        role=role,
        token=_new_token(),
        status=InviteStatus.PENDING,
        invited_by=invited_by,
        expires_at=expires,
    )
    db.add(invite)
    await db.flush()
    await db.refresh(invite)  # id / created_at from DB defaults
    return invite


async def list_pending(db: AsyncSession, org_id: uuid.UUID) -> list[Invitation]:
    res = await db.execute(
        select(Invitation)
        .where(Invitation.organization_id == org_id, Invitation.status == InviteStatus.PENDING)
        .order_by(Invitation.created_at)
    )
    return list(res.scalars().all())


async def get_invite(
    db: AsyncSession, invite_id: uuid.UUID, org_id: uuid.UUID
) -> Invitation | None:
    """Scoped by org — an invite id from another org returns None (404 in the router)."""
    res = await db.execute(
        select(Invitation).where(Invitation.id == invite_id, Invitation.organization_id == org_id)
    )
    return res.scalar_one_or_none()


async def get_by_token(db: AsyncSession, token: str) -> Invitation | None:
    res = await db.execute(select(Invitation).where(Invitation.token == token))
    return res.scalar_one_or_none()


async def revoke(db: AsyncSession, invite: Invitation) -> None:
    if invite.status is not InviteStatus.PENDING:
        raise NotPendingError("invite is not pending")
    invite.status = InviteStatus.REVOKED
    await db.flush()


async def resend(db: AsyncSession, invite: Invitation, *, ttl_hours: int) -> Invitation:
    if invite.status is not InviteStatus.PENDING:
        raise NotPendingError("invite is not pending")
    invite.token = _new_token()
    invite.expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
    await db.flush()
    return invite


async def mark_accepted(db: AsyncSession, invite: Invitation) -> None:
    invite.status = InviteStatus.ACCEPTED
    invite.accepted_at = datetime.now(UTC)
    await db.flush()
