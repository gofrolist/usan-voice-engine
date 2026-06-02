"""add calls.recording_status for egress reconciliation/observability

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-02

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE calls ADD COLUMN recording_status TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS recording_status")
