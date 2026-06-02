"""add calls.egress_id for recording egress correlation

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-02

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE calls ADD COLUMN egress_id TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS egress_id")
