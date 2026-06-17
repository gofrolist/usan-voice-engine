"""Allow-list data access for admin SSO (P3).

The admin_users table is the source of truth for who may log in and at what role.
require_admin_session re-checks it on every admin request, so a removal here revokes
access immediately. Emails are stored lowercase (the PK) for case-insensitive match.

P2 transition note: the ORM ``AdminUser`` model no longer maps a ``role`` column —
per-org role is moving to ``Membership`` (see the P2 plan, Unit A). The physical
``admin_users.role`` column still exists until migration 0033 drops it (Task A2), and
the legacy single-org admin plane still needs it. Until B/C migrate these callers,
this repo reads/writes that column through SQLAlchemy Core (``_admin_users_tbl``) so
the ORM mapper stays role-free while behavior is preserved. The role-aware reads
return lightweight ``AdminUserRow`` rows that quack like the old ORM object
(``email``/``role``/``added_by``).
"""

from typing import NamedTuple

from sqlalchemy import (
    Column,
    Enum,
    MetaData,
    Table,
    Text,
    delete,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import AdminRole

# Transitional Core view of admin_users that still includes the physical ``role``
# column (the ORM model dropped it for P2). Kept in a private MetaData so it never
# collides with the ORM Base metadata used for migrations/create_all.
_transitional_metadata = MetaData()
_admin_users_tbl = Table(
    "admin_users",
    _transitional_metadata,
    Column("email", Text, primary_key=True),
    Column("role", Enum("admin", "viewer", name="admin_role", create_type=False), nullable=False),
    Column("added_by", Text),
)


class LastAdminError(Exception):
    """Refuse an operation that would remove or demote the last remaining admin."""


class AdminUserRow(NamedTuple):
    """Lightweight, role-bearing view of an admin_users row.

    Mirrors the attributes the legacy callers read off the old ORM object
    (``email``/``role``/``added_by``) without re-mapping ``role`` onto the ORM model.
    """

    email: str
    role: AdminRole
    added_by: str | None


def _norm(email: str) -> str:
    return email.strip().lower()


def _row(email: str, role_value: str, added_by: str | None) -> AdminUserRow:
    return AdminUserRow(email=email, role=AdminRole(role_value), added_by=added_by)


async def count_admins(db: AsyncSession) -> int:
    # Callers use this in a check-then-act last-admin guard (add/remove). That is a
    # benign TOCTOU: two concurrent removals could each read count==2 and both proceed.
    # Acceptable on this single-worker, low-traffic admin plane; if hardened later, do
    # the count and the DML in one statement or under SELECT ... FOR UPDATE.
    result = await db.execute(
        select(func.count())
        .select_from(_admin_users_tbl)
        .where(_admin_users_tbl.c.role == AdminRole.ADMIN.value)
    )
    return int(result.scalar_one())


async def get_admin_user(db: AsyncSession, email: str) -> AdminUserRow | None:
    norm = _norm(email)
    result = await db.execute(
        select(
            _admin_users_tbl.c.email,
            _admin_users_tbl.c.role,
            _admin_users_tbl.c.added_by,
        ).where(_admin_users_tbl.c.email == norm)
    )
    row = result.one_or_none()
    if row is None:
        return None
    return _row(row.email, row.role, row.added_by)


async def list_admin_users(db: AsyncSession) -> list[AdminUserRow]:
    result = await db.execute(
        select(
            _admin_users_tbl.c.email,
            _admin_users_tbl.c.role,
            _admin_users_tbl.c.added_by,
        ).order_by(_admin_users_tbl.c.email)
    )
    return [_row(r.email, r.role, r.added_by) for r in result.all()]


async def add_admin_user(
    db: AsyncSession, *, email: str, role: AdminRole, added_by: str | None
) -> AdminUserRow:
    """Insert (or update the role of) an allow-listed operator. Caller commits.

    Raises LastAdminError if the upsert would demote the only remaining admin.
    """
    norm = _norm(email)
    if role is AdminRole.VIEWER:
        existing = await get_admin_user(db, norm)
        if (
            existing is not None
            and existing.role is AdminRole.ADMIN
            and await count_admins(db) <= 1
        ):
            raise LastAdminError("cannot demote the last admin")
    stmt = (
        pg_insert(_admin_users_tbl)
        .values(email=norm, role=role.value, added_by=added_by)
        # on_conflict updates ONLY role: re-adding an existing operator intentionally
        # preserves the original added_by (who first added them); the role change itself
        # is captured in the admin_audit_log by the calling route.
        .on_conflict_do_update(index_elements=["email"], set_={"role": role.value})
    )
    await db.execute(stmt)
    await db.flush()
    user = await get_admin_user(db, norm)
    assert user is not None  # just inserted/updated
    return user


async def remove_admin_user(db: AsyncSession, email: str) -> bool:
    """Delete an operator. Returns False if the email was not present. Caller commits.

    Raises LastAdminError if the target is the only remaining admin — deleting it would
    leave nobody able to perform admin mutations (an unrecoverable lockout).
    """
    norm = _norm(email)
    existing = await get_admin_user(db, norm)
    if existing is None:
        return False
    if existing.role is AdminRole.ADMIN and await count_admins(db) <= 1:
        raise LastAdminError("cannot remove the last admin")
    await db.execute(delete(_admin_users_tbl).where(_admin_users_tbl.c.email == norm))
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
            pg_insert(_admin_users_tbl)
            .values(email=norm, role=AdminRole.ADMIN.value, added_by="bootstrap")
            .on_conflict_do_nothing(index_elements=["email"])
        )
        result = await db.execute(stmt)
        inserted += getattr(result, "rowcount", 0) or 0
    await db.flush()
    return inserted
