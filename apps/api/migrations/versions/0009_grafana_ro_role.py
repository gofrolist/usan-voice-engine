"""grafana_ro: read-only role + SELECT on the six reporting tables

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-06

The login password is NOT set here (committed migrations must carry no secrets).
It is provisioned out-of-band by Terraform (google_sql_user.grafana_ro). The role
is created idempotently so this migration coexists with the Terraform-managed role
regardless of apply order. Excludes `transcripts` (raw conversation PHI) and uses
no ALTER DEFAULT PRIVILEGES, so future tables are not auto-exposed.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Idempotent create (CREATE ROLE has no IF NOT EXISTS); LOGIN, no password here.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_ro') THEN
                CREATE ROLE grafana_ro LOGIN;
            END IF;
        END
        $$
        """
    )
    # The database name "usan" is fixed in every environment (dev container + Cloud SQL).
    op.execute("GRANT CONNECT ON DATABASE usan TO grafana_ro")
    op.execute("GRANT USAGE ON SCHEMA public TO grafana_ro")
    # Least privilege: exactly the six reporting tables the dashboards read.
    # transcripts (raw conversation PHI) is intentionally NOT granted.
    op.execute(
        """
        GRANT SELECT ON
            calls, elders, wellness_logs, medication_logs, turn_metrics, call_metrics
        TO grafana_ro
        """
    )


def downgrade() -> None:
    op.execute(
        """
        REVOKE SELECT ON
            calls, elders, wellness_logs, medication_logs, turn_metrics, call_metrics
        FROM grafana_ro
        """
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM grafana_ro")
    op.execute("REVOKE CONNECT ON DATABASE usan FROM grafana_ro")
    op.execute("DROP ROLE IF EXISTS grafana_ro")
