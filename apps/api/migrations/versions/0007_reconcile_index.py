"""partial index for the recording-reconcile poller predicate

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-03

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # reconcile_missing_recordings runs every poll cycle over a table that grows
    # monotonically (retention scrubs PHI fields but never deletes Call rows), so
    # without a covering index it degrades into a full seq scan. This partial index
    # exactly matches the poller's STATIC predicate and stays tiny — it only holds
    # calls awaiting reconciliation (egress started, no result yet). Keying on
    # ended_at also serves the query's `ended_at < cutoff` filter and ORDER BY.
    op.execute(
        "CREATE INDEX idx_calls_reconcile_pending ON calls(ended_at) "
        "WHERE egress_id IS NOT NULL AND recording_uri IS NULL "
        "AND recording_status IS NULL AND ended_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_calls_reconcile_pending")
