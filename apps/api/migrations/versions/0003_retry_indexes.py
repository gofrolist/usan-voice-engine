"""retry orchestrator indexes: unique child-per-parent + due-retry partial

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-31

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # At most one retry child per parent: makes schedule_retry's idempotency
    # authoritative (race/replica-safe) instead of advisory.
    op.execute(
        "CREATE UNIQUE INDEX uq_calls_parent_call_id ON calls(parent_call_id) "
        "WHERE parent_call_id IS NOT NULL"
    )
    # Tight match for the poller's claim predicate; excludes NULL-scheduled
    # initial calls so the SKIP LOCKED scan stays small.
    op.execute(
        "CREATE INDEX idx_calls_due_retries ON calls(scheduled_at) "
        "WHERE status = 'queued' AND scheduled_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_calls_due_retries")
    op.execute("DROP INDEX IF EXISTS uq_calls_parent_call_id")
