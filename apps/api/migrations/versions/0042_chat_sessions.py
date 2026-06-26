"""chat_sessions + chat_messages: TenantScoped + FORCE RLS tables for the api_chat surface.

New owner-DDL tables (modeled on 0040). chat_status is a native enum (ongoing|ended|error).
GRANT to usan_app so the least-priv runtime role can CRUD them.

Revision ID: 0042
Revises: 0041
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORG_DEFAULT_EXPR = "COALESCE(current_setting('app.current_org', true)::uuid, default_org_id())"


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (organization_id = current_setting('app.current_org', true)::uuid) "
        f"WITH CHECK (organization_id = current_setting('app.current_org', true)::uuid)"
    )
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO usan_app")


def upgrade() -> None:
    chat_status = postgresql.ENUM(
        "ongoing", "ended", "error", name="chat_status", create_type=False
    )
    chat_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "organization_id", sa.Uuid(), server_default=sa.text(_ORG_DEFAULT_EXPR), nullable=False
        ),
        sa.Column("agent_profile_id", sa.Uuid(), nullable=False),
        sa.Column("agent_version", sa.Integer(), nullable=False),
        sa.Column("status", chat_status, server_default="ongoing", nullable=False),
        sa.Column("chat_type", sa.Text(), server_default=sa.text("'api_chat'"), nullable=False),
        sa.Column(
            "dynamic_vars", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column(
            "custom_attributes", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["agent_profile_id"], ["agent_profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_sessions_organization_id", "chat_sessions", ["organization_id"])
    op.create_index("ix_chat_sessions_started_at_id", "chat_sessions", ["started_at", "id"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "organization_id", sa.Uuid(), server_default=sa.text(_ORG_DEFAULT_EXPR), nullable=False
        ),
        sa.Column("chat_session_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["chat_session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_session_id", "seq", name="uq_chat_messages_session_seq"),
    )
    op.create_index("ix_chat_messages_organization_id", "chat_messages", ["organization_id"])
    op.create_index("ix_chat_messages_session_seq", "chat_messages", ["chat_session_id", "seq"])

    _enable_rls("chat_sessions")
    _enable_rls("chat_messages")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON chat_messages")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON chat_sessions")
    op.drop_index("ix_chat_messages_session_seq", table_name="chat_messages")
    op.drop_index("ix_chat_messages_organization_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chat_sessions_started_at_id", table_name="chat_sessions")
    op.drop_index("ix_chat_sessions_organization_id", table_name="chat_sessions")
    op.drop_table("chat_sessions")
    postgresql.ENUM(name="chat_status", create_type=False).drop(op.get_bind(), checkfirst=True)
