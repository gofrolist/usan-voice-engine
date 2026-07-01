"""chat_sessions.flow_current_node_id: the conversation-flow DAG cursor (6-runtime-chat).

Additive nullable column on the existing chat_sessions table. The table-level GRANT to
usan_app already covers future columns, so no re-grant is needed. Inert until
FLOW_RUNTIME_ENABLED is set. Owner-DDL (ALTER TABLE runs as the usan owner on deploy).

Revision ID: 0050
Revises: 0049
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_sessions", sa.Column("flow_current_node_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_sessions", "flow_current_node_id")
