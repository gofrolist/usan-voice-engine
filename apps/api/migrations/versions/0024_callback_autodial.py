"""callback auto-dial: widen status CHECK (+scheduled/dialed) + dispatched_call_id/profile_override

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-15

US8 / T073. The callback dialer materializes an outbound Call for a due ``open`` callback,
advancing it ``open -> scheduled`` (a Call now exists) then ``scheduled -> dialed`` once
that Call leaves the queue. ``dispatched_call_id`` links the materialized Call;
``profile_override`` lets a Spanish callback (set_spanish_callback) carry a Spanish agent
profile. A composite index serves the dialer's due-claim scan.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Widen the ops-queue status CHECK with the two dial-workflow states (0013 set the
    # original three). Drop-then-recreate: Postgres has no "alter constraint".
    op.execute(
        "ALTER TABLE callback_requests DROP CONSTRAINT IF EXISTS ck_callback_requests_status"
    )
    op.execute(
        """
        ALTER TABLE callback_requests
            ADD CONSTRAINT ck_callback_requests_status
                CHECK (status IN ('open', 'acknowledged', 'resolved', 'scheduled', 'dialed'))
        """
    )

    # 2. The Call materialized for this callback (SET NULL so a retention purge of the call
    # keeps the callback record), and the agent profile a Spanish callback should dial with.
    op.execute(
        """
        ALTER TABLE callback_requests
            ADD COLUMN dispatched_call_id UUID REFERENCES calls(id) ON DELETE SET NULL,
            ADD COLUMN profile_override UUID REFERENCES agent_profiles(id) ON DELETE SET NULL
        """
    )

    # 3. Serve the dialer's due-claim scan: open callbacks with a parsed time, oldest first.
    op.execute("CREATE INDEX idx_callback_requests_due ON callback_requests (status, requested_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_callback_requests_due")
    op.execute("ALTER TABLE callback_requests DROP COLUMN IF EXISTS profile_override")
    op.execute("ALTER TABLE callback_requests DROP COLUMN IF EXISTS dispatched_call_id")
    op.execute(
        "ALTER TABLE callback_requests DROP CONSTRAINT IF EXISTS ck_callback_requests_status"
    )
    op.execute(
        """
        ALTER TABLE callback_requests
            ADD CONSTRAINT ck_callback_requests_status
                CHECK (status IN ('open', 'acknowledged', 'resolved'))
        """
    )
