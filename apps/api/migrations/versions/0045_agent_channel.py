"""agent_profiles: add channel discriminator (voice|chat) for the chat-agent overlay (Phase 4c-1).

Additive. Distinguishes voice agents (channel='voice' — the default/backfill) from chat agents
(channel='chat', created via the compat create-chat-agent path). The new column inherits the
table's existing usan_app GRANT + RLS policy, so no new grant/policy is needed. Owner-DDL: the
deploy migrates as the usan owner. Inert until a v* tag.

Revision ID: 0045
Revises: 0044
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_profiles",
        sa.Column("channel", sa.Text(), nullable=False, server_default=sa.text("'voice'")),
    )


def downgrade() -> None:
    op.drop_column("agent_profiles", "channel")
