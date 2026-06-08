"""Allow-list data access for admin SSO (P3).

The admin_users table is the source of truth for who may log in and at what role.
require_admin_session re-checks it on every admin request, so a removal here revokes
access immediately. Emails are stored lowercase (the PK) for case-insensitive match.
"""

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import AdminRole
from usan_api.db.models import AdminUser


class LastAdminError(Exception):
    """Refuse an operation that would remove or demote the last remaining admin."""


def _norm(email: str) -> str:
    return email.strip().lower()


async def count_admins(db: AsyncSession) -> int:
    result = await db.execute(
        select(func.count()).select_from(AdminUser).where(AdminUser.role == AdminRole.ADMIN)
    )
    return int(result.scalar_one())


async def get_admin_user(db: AsyncSession, email: str) -> AdminUser | None:
    return await db.get(AdminUser, _norm(email))


async def list_admin_users(db: AsyncSession) -> list[AdminUser]:
    result = await db.execute(select(AdminUser).order_by(AdminUser.email))
    return list(result.scalars().all())


async def add_admin_user(
    db: AsyncSession, *, email: str, role: AdminRole, added_by: str | None
) -> AdminUser:
    """Insert (or update the role of) an allow-listed operator. Caller commits.

    Raises LastAdminError if the upsert would demote the only remaining admin.
    """
    norm = _norm(email)
    if role is AdminRole.VIEWER:
        existing = await db.get(AdminUser, norm)
        if (
            existing is not None
            and existing.role is AdminRole.ADMIN
            and await count_admins(db) <= 1
        ):
            raise LastAdminError("cannot demote the last admin")
    stmt = (
        pg_insert(AdminUser)
        .values(email=norm, role=role, added_by=added_by)
        .on_conflict_do_update(index_elements=["email"], set_={"role": role})
    )
    await db.execute(stmt)
    await db.flush()
    user = await db.get(AdminUser, norm)
    assert user is not None  # just inserted/updated
    return user


async def remove_admin_user(db: AsyncSession, email: str) -> bool:
    """Delete an operator. Returns False if the email was not present. Caller commits.

    Raises LastAdminError if the target is the only remaining admin — deleting it would
    leave nobody able to perform admin mutations (an unrecoverable lockout).
    """
    norm = _norm(email)
    existing = await db.get(AdminUser, norm)
    if existing is None:
        return False
    if existing.role is AdminRole.ADMIN and await count_admins(db) <= 1:
        raise LastAdminError("cannot remove the last admin")
    await db.execute(delete(AdminUser).where(AdminUser.email == norm))
    await db.flush()
    return True


async def seed_bootstrap(db: AsyncSession, emails: list[str]) -> int:
    """Insert any missing bootstrap emails as admins. Returns the count inserted.

    Idempotent: ON CONFLICT DO NOTHING leaves existing rows (and their possibly
    edited roles) untouched. Caller commits.
    """
    inserted = 0
    for email in emails:
        norm = _norm(email)
        if not norm:
            continue
        stmt = (
            pg_insert(AdminUser)
            .values(email=norm, role=AdminRole.ADMIN, added_by="bootstrap")
            .on_conflict_do_nothing(index_elements=["email"])
        )
        result = await db.execute(stmt)
        inserted += getattr(result, "rowcount", 0) or 0
    await db.flush()
    return inserted
