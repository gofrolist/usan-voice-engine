"""P3: invitations table (pending-token org invites). Global, non-RLS control-plane
table joined to organizations; looked up by token before any org context exists.

Revision ID: 0035
Revises: 0034
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New enum type. create_type=False suppresses auto-DDL when the column is built inside
    # create_table below; the explicit .create(checkfirst=True) here is what actually
    # creates the type. (admin_role already exists from 0010 — only referenced below with
    # create_type=False, never re-created.)
    invite_status = postgresql.ENUM(
        "pending", "accepted", "revoked", name="invite_status", create_type=False
    )
    invite_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "invitations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM("admin", "viewer", name="admin_role", create_type=False),
            nullable=False,
        ),
        sa.Column("token", sa.Text(), nullable=False),
        sa.Column("status", invite_status, nullable=False, server_default="pending"),
        sa.Column("invited_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("uq_invitations_token", "invitations", ["token"], unique=True)
    op.create_index("ix_invitations_organization_id", "invitations", ["organization_id"])
    # One live invite per email per org. email is invariably stored lowercased by the
    # repository (_norm), so indexing the raw column is equivalent to lower(email) for
    # every application write.
    op.create_index(
        "uq_invitations_org_email_pending",
        "invitations",
        ["organization_id", "email"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    # The non-superuser app role (RLS subject) must be able to use the table.
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON invitations TO usan_app")


def downgrade() -> None:
    op.drop_index("uq_invitations_org_email_pending", table_name="invitations")
    op.drop_index("ix_invitations_organization_id", table_name="invitations")
    op.drop_index("uq_invitations_token", table_name="invitations")
    op.drop_table("invitations")
    postgresql.ENUM(name="invite_status", create_type=False).drop(op.get_bind(), checkfirst=True)
