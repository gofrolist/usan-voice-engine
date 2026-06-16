"""organization_id + RLS on the remaining 27 tenant tables

Revision ID: 0032
Revises: 0031
Create Date: 2026-06-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# contacts handled in 0031. admin_users + organizations stay global.
_TABLES = [
    "dnc_list",
    "calls",
    "transcripts",
    "wellness_logs",
    "medication_logs",
    "medication_reminders",
    "personal_facts",
    "conversation_summaries",
    "wellbeing_survey_results",
    "activity_history",
    "turn_metrics",
    "call_metrics",
    "agent_profiles",
    "agent_profile_versions",
    "call_schedules",
    "call_batches",
    "call_batch_targets",
    "webhook_endpoints",
    "webhook_deliveries",
    "custom_variables",
    "admin_audit_log",
    "follow_up_flags",
    "callback_requests",
    "sms_messages",
    "family_contacts",
    "family_tasks",
    "family_reports",
]
_DEFAULT_ORG = "(SELECT id FROM organizations WHERE slug = 'usan')"
# Same function-based DEFAULT as 0031: Postgres forbids a subquery in a DEFAULT
# expression, so default_org_id() (created in 0031) encapsulates the lookup. COALESCE
# prefers the request context; falls back to the default org for context-free seeds.
_ORG_DEFAULT_EXPR = "COALESCE(current_setting('app.current_org', true)::uuid, default_org_id())"


def upgrade() -> None:
    for t in _TABLES:
        op.add_column(t, sa.Column("organization_id", sa.Uuid(), nullable=True))
        # The UPDATE is a full-table scan + rewrite under ACCESS EXCLUSIVE, run once per
        # table in this loop. Acceptable while prod row counts are small (pre-RetellAI
        # cutover). If a table ever holds >~500k rows (calls/transcripts/turn_metrics),
        # switch to a batched backfill with a lock_timeout before running this in prod.
        # Backfill from a hardcoded constant (no user input); S608 is a false positive.
        op.execute(f"UPDATE {t} SET organization_id = {_DEFAULT_ORG} WHERE organization_id IS NULL")  # noqa: S608, E501
        op.alter_column(t, "organization_id", nullable=False)
        op.execute(f"ALTER TABLE {t} ALTER COLUMN organization_id SET DEFAULT {_ORG_DEFAULT_EXPR}")
        op.create_foreign_key(
            f"fk_{t}_organization", t, "organizations", ["organization_id"], ["id"]
        )
        op.create_index(f"ix_{t}_organization_id", t, ["organization_id"])
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {t} "
            f"USING (organization_id = current_setting('app.current_org', true)::uuid) "
            f"WITH CHECK (organization_id = current_setting('app.current_org', true)::uuid)"
        )


def downgrade() -> None:
    for t in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
        op.drop_index(f"ix_{t}_organization_id", table_name=t)
        op.drop_constraint(f"fk_{t}_organization", t, type_="foreignkey")
        op.drop_column(t, "organization_id")
