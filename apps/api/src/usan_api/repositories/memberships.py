"""Per-org membership data access (P2). memberships + admin_users are GLOBAL
(non-RLS) control-plane tables, so every query here MUST be scoped by
organization_id in app code — that scoping replaces RLS for these tables."""

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import AdminRole
from usan_api.db.models import Membership
from usan_api.repositories import admin_users as admin_users_repo


class LastOrgAdminError(Exception):
    """Refuse removing/demoting the last ADMIN of an org (unrecoverable lockout)."""


def _norm(email: str) -> str:
    return email.strip().lower()


async def list_memberships_for_email(db: AsyncSession, email: str) -> list[Membership]:
    res = await db.execute(
        select(Membership).where(Membership.email == _norm(email)).order_by(Membership.created_at)
    )
    return list(res.scalars().all())


async def get_membership(db: AsyncSession, email: str, org_id: uuid.UUID) -> Membership | None:
    return await db.get(Membership, (_norm(email), org_id))


async def list_members(db: AsyncSession, org_id: uuid.UUID) -> list[Membership]:
    res = await db.execute(
        select(Membership).where(Membership.organization_id == org_id).order_by(Membership.email)
    )
    return list(res.scalars().all())


async def count_org_admins(db: AsyncSession, org_id: uuid.UUID) -> int:
    res = await db.execute(
        select(func.count())
        .select_from(Membership)
        .where(Membership.organization_id == org_id, Membership.role == AdminRole.ADMIN)
    )
    return int(res.scalar_one())


async def _locked_org_admin_count(db: AsyncSession, org_id: uuid.UUID) -> int:
    """Count an org's ADMIN memberships while holding a row lock on each.

    The last-admin guard is a read-then-write: a plain count lets two concurrent
    demotions/removals both observe ``2`` and both proceed, leaving the org with zero
    admins (an unrecoverable lockout). Locking the ADMIN rows ``FOR UPDATE`` serializes
    those transactions — the second blocks until the first commits, then re-reads the
    now-reduced count and is correctly refused. ``FOR UPDATE`` is not allowed with an
    aggregate, so we lock the rows and count them in Python.
    """
    res = await db.execute(
        select(Membership.email)
        .where(Membership.organization_id == org_id, Membership.role == AdminRole.ADMIN)
        .with_for_update()
    )
    return len(res.scalars().all())


async def add_member(
    db: AsyncSession,
    *,
    email: str,
    org_id: uuid.UUID,
    role: AdminRole,
    added_by: str | None,
) -> Membership:
    norm = _norm(email)
    await admin_users_repo.ensure_identity(db, email=norm)  # FK target must exist
    stmt = (
        pg_insert(Membership)
        .values(email=norm, organization_id=org_id, role=role, added_by=added_by)
        .on_conflict_do_update(index_elements=["email", "organization_id"], set_={"role": role})
    )
    await db.execute(stmt)
    await db.flush()
    m = await db.get(Membership, (norm, org_id))
    assert m is not None
    return m


async def set_member_role(
    db: AsyncSession,
    *,
    email: str,
    org_id: uuid.UUID,
    role: AdminRole,
) -> Membership:
    norm = _norm(email)
    m = await db.get(Membership, (norm, org_id))
    if m is None:
        raise KeyError("membership not found")
    if (
        m.role is AdminRole.ADMIN
        and role is AdminRole.VIEWER
        and await _locked_org_admin_count(db, org_id) <= 1
    ):
        raise LastOrgAdminError("cannot demote the last admin of this org")
    m.role = role
    await db.flush()
    return m


async def remove_member(db: AsyncSession, *, email: str, org_id: uuid.UUID) -> bool:
    norm = _norm(email)
    m = await db.get(Membership, (norm, org_id))
    if m is None:
        return False
    if m.role is AdminRole.ADMIN and await _locked_org_admin_count(db, org_id) <= 1:
        raise LastOrgAdminError("cannot remove the last admin of this org")
    await db.execute(
        delete(Membership).where(Membership.email == norm, Membership.organization_id == org_id)
    )
    await db.flush()
    return True
