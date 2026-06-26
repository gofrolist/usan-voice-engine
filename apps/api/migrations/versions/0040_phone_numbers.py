"""phone_numbers: per-org TenantScoped + FORCE RLS table for the compat phone-number surface.

New owner-DDL table (modeled on 0037). Bindings (inbound/outbound[/_sms]_agents) are stored as
JSONB AgentWeight lists; sip_auth_password is write-only (never echoed). GRANT to usan_app so the
least-priv runtime role can CRUD it.

Revision ID: 0040
Revises: 0039
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040"
down_revision: str | None = "0039"
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
        "phone_numbers",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "organization_id", sa.Uuid(), server_default=sa.text(_ORG_DEFAULT_EXPR), nullable=False
        ),
        sa.Column("phone_e164", sa.Text(), nullable=False),
        sa.Column("phone_number_type", sa.Text(), nullable=False),
        sa.Column("phone_number_pretty", sa.Text(), nullable=True),
        sa.Column("nickname", sa.Text(), nullable=True),
        sa.Column("area_code", sa.Integer(), nullable=True),
        sa.Column("inbound_webhook_url", sa.Text(), nullable=True),
        sa.Column("inbound_sms_webhook_url", sa.Text(), nullable=True),
        sa.Column("allowed_inbound_country_list", sa.ARRAY(sa.Text()), nullable=True),
        sa.Column("allowed_outbound_country_list", sa.ARRAY(sa.Text()), nullable=True),
        sa.Column("fallback_number", sa.Text(), nullable=True),
        sa.Column("transport", sa.Text(), nullable=True),
        sa.Column("termination_uri", sa.Text(), nullable=True),
        sa.Column("sip_auth_username", sa.Text(), nullable=True),
        sa.Column("sip_auth_password", sa.Text(), nullable=True),
        sa.Column("inbound_agents", postgresql.JSONB(), nullable=True),
        sa.Column("outbound_agents", postgresql.JSONB(), nullable=True),
        sa.Column("inbound_sms_agents", postgresql.JSONB(), nullable=True),
        sa.Column("outbound_sms_agents", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("phone_e164", "organization_id", name="uq_phone_numbers_e164_org"),
    )
    op.create_index("ix_phone_numbers_organization_id", "phone_numbers", ["organization_id"])
    # Keyset list order (created_at, id); index it for the list endpoint.
    op.create_index("ix_phone_numbers_created_at_id", "phone_numbers", ["created_at", "id"])
    _enable_rls("phone_numbers")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON phone_numbers")
    op.drop_index("ix_phone_numbers_created_at_id", table_name="phone_numbers")
    op.drop_index("ix_phone_numbers_organization_id", table_name="phone_numbers")
    op.drop_table("phone_numbers")
