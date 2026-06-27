"""chat_messages: add provider_message_id + partial-unique dedup; sms-match index (Phase 4b-2).

Additive. provider_message_id is the Telnyx inbound message id used to dedup redeliveries
of an inbound SMS reply; the partial unique (organization_id, provider_message_id) only
constrains non-NULL rows so api_chat messages (NULL) are unaffected. ix_chat_sessions_sms_match
speeds the per-inbound open-sms_chat lookup. Owner-DDL migration — the deploy migrates as
the usan owner; the new column inherits the table's usan_app GRANT + RLS policy.

Revision ID: 0044
Revises: 0043
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("provider_message_id", sa.Text(), nullable=True))
    op.create_index(
        "uq_chat_messages_provider_msg",
        "chat_messages",
        ["organization_id", "provider_message_id"],
        unique=True,
        postgresql_where=sa.text("provider_message_id IS NOT NULL"),
    )
    op.create_index(
        "ix_chat_sessions_sms_match",
        "chat_sessions",
        ["from_number", "to_number"],
        postgresql_where=sa.text("chat_type = 'sms_chat'"),
    )


def downgrade() -> None:
    op.drop_index("ix_chat_sessions_sms_match", table_name="chat_sessions")
    op.drop_index("uq_chat_messages_provider_msg", table_name="chat_messages")
    op.drop_column("chat_messages", "provider_message_id")
