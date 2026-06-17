"""identity columns + memberships table + role data-migration

Revision ID: 0033
Revises: 0032
Create Date: 2026-06-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_USAN = "(SELECT id FROM organizations WHERE slug = 'usan')"


def upgrade() -> None:
    # 1. Identity columns on admin_users (global table; no RLS).
    op.add_column(
        "admin_users",
        sa.Column("is_super_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "admin_users",
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
    )
    op.add_column("admin_users", sa.Column("last_active_org_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_admin_users_last_org",
        "admin_users",
        "organizations",
        ["last_active_org_id"],
        ["id"],
    )

    # 2. memberships table (global, non-RLS).
    op.create_table(
        "memberships",
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column(
            # postgresql.ENUM (not sa.Enum) so create_type=False survives the
            # adapt-to-native step inside op.create_table — a plain sa.Enum drops
            # the flag and re-emits CREATE TYPE admin_role (already created in 0010).
            "role",
            postgresql.ENUM("admin", "viewer", name="admin_role", create_type=False),
            nullable=False,
        ),
        sa.Column("added_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["email"], ["admin_users.email"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("email", "organization_id"),
    )

    # 3. Data-migrate: each existing operator gets a usan membership at their current role.
    # S608: the only interpolation is the constant org subquery (_USAN) — no user input.
    migrate_roles = (
        f"INSERT INTO memberships (email, organization_id, role, added_by) "  # noqa: S608
        f"SELECT email, {_USAN}, role, 'migration' FROM admin_users "
        f"ON CONFLICT DO NOTHING"
    )
    op.execute(migrate_roles)

    # 4. Drop the per-person role column (now per-membership).
    op.drop_column("admin_users", "role")

    # 5. Grant the non-superuser app role access to the new table (mirror 0030's grant form).
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON memberships TO usan_app")


def downgrade() -> None:
    op.add_column(
        "admin_users",
        sa.Column(
            "role",
            postgresql.ENUM("admin", "viewer", name="admin_role", create_type=False),
            nullable=False,
            server_default="admin",
        ),
    )
    # S608: the only interpolation is the constant org subquery (_USAN) — no user input.
    restore_roles = (
        f"UPDATE admin_users a SET role = m.role FROM memberships m "  # noqa: S608
        f"WHERE m.email = a.email AND m.organization_id = {_USAN}"
    )
    op.execute(restore_roles)
    op.drop_table("memberships")
    op.drop_constraint("fk_admin_users_last_org", "admin_users", type_="foreignkey")
    op.drop_column("admin_users", "last_active_org_id")
    op.drop_column("admin_users", "status")
    op.drop_column("admin_users", "is_super_admin")
