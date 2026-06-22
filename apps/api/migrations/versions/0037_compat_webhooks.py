"""Feature 003 / US2: compat (RetellAI) webhook subscriptions + delivery outbox.

Two TENANT-scoped tables (organization_id + RLS, mirroring 0032), deliberately SEPARATE
from the native webhook_endpoints/webhook_deliveries so the native delivery poller (which
claims ALL due rows and signs them X-Usan) never touches a compat row, and a compat
circuit-breaker trip can never disable a native endpoint (and vice versa). The compat
poller signs the Retell ``x-retell-signature`` scheme with a per-subscription secret.

- ``compat_webhook_endpoints``: one agent's webhook subscription (webhook_url +
  webhook_events), unique per (org, agent_profile). Carries its own dedicated signing
  ``secret`` (returned once at registration; the CRM passes IT — not its API key — to
  retell-sdk ``verify()``).
- ``compat_webhook_deliveries``: the transactional outbox row; payload is the MINIMAL
  ``{event, call_id}`` reference — the full ``{event, call}`` body is assembled at delivery
  time by the compat poller (off the hot path) so it reflects the latest call state.

Revision ID: 0037
Revises: 0036
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same function-based DEFAULT as 0031/0032: Postgres forbids a subquery in a DEFAULT, so the
# default-org fallback is wrapped in default_org_id(). COALESCE prefers the request/worker
# context; falls back to the default org for context-free (superuser) seeds.
_ORG_DEFAULT_EXPR = "COALESCE(current_setting('app.current_org', true)::uuid, default_org_id())"
_EVENTS = "ARRAY['call_started','call_ended','call_analyzed']::text[]"


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (organization_id = current_setting('app.current_org', true)::uuid) "
        f"WITH CHECK (organization_id = current_setting('app.current_org', true)::uuid)"
    )
    # The non-superuser app role (RLS subject) must be able to use the table.
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO usan_app")


def upgrade() -> None:
    op.create_table(
        "compat_webhook_endpoints",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            server_default=sa.text(_ORG_DEFAULT_EXPR),
            nullable=False,
        ),
        sa.Column("agent_profile_id", sa.Uuid(), nullable=False),
        sa.Column("webhook_url", sa.Text(), nullable=False),
        # Subset of the closed event enum; CHECK keeps an unknown event name out of the column.
        sa.Column("webhook_events", sa.ARRAY(sa.Text()), nullable=False),
        # 64 hex chars, server-generated, returned once at registration, NEVER logged.
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "consecutive_failures", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("disabled_reason", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["agent_profile_id"], ["agent_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One subscription per agent per org — the registration UPSERT target.
        sa.UniqueConstraint("organization_id", "agent_profile_id", name="uq_compat_webhook_agent"),
        sa.CheckConstraint(f"webhook_events <@ {_EVENTS}", name="ck_compat_webhook_events"),
    )
    op.create_index(
        "ix_compat_webhook_endpoints_organization_id",
        "compat_webhook_endpoints",
        ["organization_id"],
    )
    op.create_index(
        "ix_compat_webhook_endpoints_agent_profile_id",
        "compat_webhook_endpoints",
        ["agent_profile_id"],
    )
    _enable_rls("compat_webhook_endpoints")

    op.create_table(
        "compat_webhook_deliveries",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            server_default=sa.text(_ORG_DEFAULT_EXPR),
            nullable=False,
        ),
        sa.Column("endpoint_id", sa.Uuid(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["endpoint_id"], ["compat_webhook_endpoints.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')", name="ck_compat_delivery_status"
        ),
    )
    op.create_index(
        "ix_compat_webhook_deliveries_organization_id",
        "compat_webhook_deliveries",
        ["organization_id"],
    )
    op.create_index(
        "ix_compat_webhook_deliveries_endpoint",
        "compat_webhook_deliveries",
        ["endpoint_id"],
    )
    # Partial index keeps claim_due O(due): only pending rows are ever scanned by next_attempt_at.
    op.create_index(
        "ix_compat_webhook_deliveries_due",
        "compat_webhook_deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    _enable_rls("compat_webhook_deliveries")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON compat_webhook_deliveries")
    op.drop_index("ix_compat_webhook_deliveries_due", table_name="compat_webhook_deliveries")
    op.drop_index("ix_compat_webhook_deliveries_endpoint", table_name="compat_webhook_deliveries")
    op.drop_index(
        "ix_compat_webhook_deliveries_organization_id", table_name="compat_webhook_deliveries"
    )
    op.drop_table("compat_webhook_deliveries")

    op.execute("DROP POLICY IF EXISTS tenant_isolation ON compat_webhook_endpoints")
    op.drop_index(
        "ix_compat_webhook_endpoints_agent_profile_id", table_name="compat_webhook_endpoints"
    )
    op.drop_index(
        "ix_compat_webhook_endpoints_organization_id", table_name="compat_webhook_endpoints"
    )
    op.drop_table("compat_webhook_endpoints")
