"""chat_analyses: TenantScoped + FORCE RLS table for post-chat analysis (Phase 4c-2).

New owner-DDL table (modeled on 0042). One row per chat (chat_session_id UNIQUE → the
rerun-chat-analysis op upserts in place). GRANT to usan_app so the least-priv runtime role
can CRUD it. Additive + inert until a v* tag.

Revision ID: 0046
Revises: 0045
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046"
down_revision: str | None = "0045"
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
    op.create_table(
        "chat_analyses",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "organization_id", sa.Uuid(), server_default=sa.text(_ORG_DEFAULT_EXPR), nullable=False
        ),
        sa.Column("chat_session_id", sa.Uuid(), nullable=False),
        sa.Column("chat_summary", sa.Text(), nullable=True),
        sa.Column("user_sentiment", sa.Text(), nullable=True),
        sa.Column("chat_successful", sa.Boolean(), nullable=True),
        sa.Column("custom_analysis_data", postgresql.JSONB(), nullable=True),
        sa.Column("model_version", sa.Text(), server_default=sa.text("''"), nullable=False),
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
        sa.ForeignKeyConstraint(["chat_session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_session_id", name="uq_chat_analyses_session"),
    )
    op.create_index("ix_chat_analyses_organization_id", "chat_analyses", ["organization_id"])
    _enable_rls("chat_analyses")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON chat_analyses")
    op.drop_index("ix_chat_analyses_organization_id", table_name="chat_analyses")
    op.drop_table("chat_analyses")
