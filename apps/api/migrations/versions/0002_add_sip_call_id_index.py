"""add partial index on calls.sip_call_id

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-30

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX idx_calls_sip_call_id ON calls(sip_call_id) WHERE sip_call_id IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_calls_sip_call_id")
