"""conversation_flow_components: TenantScoped + FORCE RLS table for RetellAI component CRUD (6b).

New owner-DDL table (modeled on 0048). Plain per-org table — no cross-org accessor — so it
uses FORCE RLS (NOT the 0047 KB ENABLE-only exception). Stores the persisted-not-honored
component body as JSONB. No version column: the oracle ConversationFlowComponentResponse has
none. GRANT to usan_app so the least-priv runtime role can CRUD it. Additive + inert.

Revision ID: 0049
Revises: 0048
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0049"
down_revision: str | None = "0048"
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
        "conversation_flow_components",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "organization_id", sa.Uuid(), server_default=sa.text(_ORG_DEFAULT_EXPR), nullable=False
        ),
        sa.Column("config", postgresql.JSONB(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_conversation_flow_components_organization_id",
        "conversation_flow_components",
        ["organization_id"],
    )
    _enable_rls("conversation_flow_components")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON conversation_flow_components")
    op.drop_index(
        "ix_conversation_flow_components_organization_id",
        table_name="conversation_flow_components",
    )
    op.drop_table("conversation_flow_components")
