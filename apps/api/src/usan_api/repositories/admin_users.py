"""Global identity data access for admin SSO (P2).

The admin_users table is the global source of truth for *who* the person is (their
identity + super-admin flag + status). Per-org *role* now lives in ``memberships``
(see repositories/memberships.py). require_admin_session re-checks both on every
admin request, so a status change or membership removal revokes access immediately.
Emails are stored lowercase (the PK) for case-insensitive match.

P2: the ORM ``AdminUser`` model is identity-only (no ``role`` column — migration 0033
moved role to ``memberships`` and dropped the physical column). This repo therefore
reads/writes through the ORM model directly.
"""

import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import AdminRole
from usan_api.db.models import AdminUser


def _norm(email: str) -> str:
    return email.strip().lower()


async def get_admin_user(db: AsyncSession, email: str) -> AdminUser | None:
    """The global identity row for an email (or None). Identity only — no role."""
    return await db.get(AdminUser, _norm(email))


async def ensure_identity(
    db: AsyncSession, *, email: str, is_super_admin: bool = False
) -> AdminUser:
    """Insert the identity if missing (idempotent); return it. Caller commits.

    Used by membership creation (the FK target must exist) and by SSO invites. An
    existing row is left untouched (its ``is_super_admin``/``status`` are not
    downgraded by a plain invite).
    """
    norm = _norm(email)
    stmt = (
        pg_insert(AdminUser)
        .values(email=norm, is_super_admin=is_super_admin, added_by="invite")
        .on_conflict_do_nothing(index_elements=["email"])
    )
    await db.execute(stmt)
    await db.flush()
    user = await db.get(AdminUser, norm)
    assert user is not None
    return user


async def set_last_active_org(db: AsyncSession, *, email: str, org_id: uuid.UUID) -> None:
    """Remember the org the person last switched to (drives login default). Caller commits."""
    user = await db.get(AdminUser, _norm(email))
    if user is not None:
        user.last_active_org_id = org_id
        await db.flush()


async def seed_bootstrap(db: AsyncSession, emails: list[str]) -> int:
    """Ensure the bootstrap emails are super-admin identities with a usan ADMIN
    membership. Returns the count of identities created. Idempotent. Caller commits.

    Without at least one bootstrap super-admin, nobody can log in via SSO.
    """
    # Lazy import: memberships imports this module, so importing it at module scope
    # would create a circular import.
    from usan_api.repositories import memberships as memberships_repo
    from usan_api.repositories import organizations as organizations_repo

    created = 0
    usan = await organizations_repo.get_org_by_slug(db, "usan")
    for email in emails:
        norm = _norm(email)
        if not norm:
            continue
        existed = await db.get(AdminUser, norm) is not None
        await ensure_identity(db, email=norm, is_super_admin=True)
        if not existed:
            created += 1
        if usan is not None:
            await memberships_repo.add_member(
                db, email=norm, org_id=usan.id, role=AdminRole.ADMIN, added_by="bootstrap"
            )
    await db.flush()
    return created
