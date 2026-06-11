"""ops-queue status workflow: status_updated_at/by + status CHECKs + idx_calls_created

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-10

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Ops-queue status workflow columns (NULL = never transitioned past 'open').
    op.execute(
        """
        ALTER TABLE follow_up_flags
            ADD COLUMN status_updated_at TIMESTAMPTZ,
            ADD COLUMN status_updated_by TEXT
        """
    )  # status_updated_by: admin actor email
    op.execute(
        """
        ALTER TABLE callback_requests
            ADD COLUMN status_updated_at TIMESTAMPTZ,
            ADD COLUMN status_updated_by TEXT
        """
    )

    # 2. Constrain the status enum (PR #54 review gap: unconstrained TEXT today).
    # Defensive normalize first: the only writer ever was the server_default 'open',
    # but a stray manual edit must not abort the deploy's auto-migration.
    op.execute(
        """
        UPDATE follow_up_flags SET status = 'open'
            WHERE status NOT IN ('open', 'acknowledged', 'resolved')
        """
    )
    op.execute(
        """
        ALTER TABLE follow_up_flags
            ADD CONSTRAINT ck_follow_up_flags_status
                CHECK (status IN ('open', 'acknowledged', 'resolved'))
        """
    )
    op.execute(
        """
        UPDATE callback_requests SET status = 'open'
            WHERE status NOT IN ('open', 'acknowledged', 'resolved')
        """
    )
    op.execute(
        """
        ALTER TABLE callback_requests
            ADD CONSTRAINT ck_callback_requests_status
                CHECK (status IN ('open', 'acknowledged', 'resolved'))
        """
    )

    # 3. The global "all calls, newest first" admin list has no serving index today
    # (idx_calls_elder covers only the per-elder slice). Composite (created_at, id)
    # because created_at ties are guaranteed (func.now() is the transaction timestamp
    # and the A1 batch materializer inserts many Call rows per poller transaction),
    # and the list orders by the same pair.
    op.execute("CREATE INDEX idx_calls_created ON calls (created_at DESC, id DESC)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_calls_created")
    op.execute(
        "ALTER TABLE callback_requests DROP CONSTRAINT IF EXISTS ck_callback_requests_status"
    )
    op.execute("ALTER TABLE callback_requests DROP COLUMN IF EXISTS status_updated_by")
    op.execute("ALTER TABLE callback_requests DROP COLUMN IF EXISTS status_updated_at")
    op.execute("ALTER TABLE follow_up_flags DROP CONSTRAINT IF EXISTS ck_follow_up_flags_status")
    op.execute("ALTER TABLE follow_up_flags DROP COLUMN IF EXISTS status_updated_by")
    op.execute("ALTER TABLE follow_up_flags DROP COLUMN IF EXISTS status_updated_at")
