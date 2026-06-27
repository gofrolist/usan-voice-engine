"""chat_sessions: add nullable from_number/to_number for sms_chat rows (Phase 4b-1).

Additive columns on the existing chat_sessions table. Nullable, so api_chat rows are
unaffected and the columns inherit the table's existing usan_app GRANT + RLS policy
(no new grant/policy needed). Owner-DDL migration — the deploy migrates as the usan owner.

Revision ID: 0043
Revises: 0042
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("from_number", sa.Text(), nullable=True))
    op.add_column("chat_sessions", sa.Column("to_number", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "to_number")
    op.drop_column("chat_sessions", "from_number")
