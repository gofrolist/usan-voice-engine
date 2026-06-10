"""phase-3 tool tables: follow_up_flags, callback_requests, sms_messages

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-09

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # follow_up_flags: human-reviewed safety escalations (spec §5.1).
    # CASCADE to calls (a deleted call drops its flags); NO cascade to elders
    # (an elder delete must not silently erase clinical follow-up history).
    op.execute(
        """
        CREATE TABLE follow_up_flags (
            id          BIGSERIAL PRIMARY KEY,
            call_id     UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            elder_id    UUID NOT NULL REFERENCES elders(id),
            severity    TEXT NOT NULL,
            category    TEXT NOT NULL,
            reason      TEXT,
            status      TEXT NOT NULL DEFAULT 'open',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_followup_flags_elder ON follow_up_flags(elder_id, created_at DESC)"
    )
    op.execute("CREATE INDEX idx_followup_flags_status ON follow_up_flags(status, created_at DESC)")

    # callback_requests: durable call-back asks for a human to action (spec §5.2).
    op.execute(
        """
        CREATE TABLE callback_requests (
            id                  BIGSERIAL PRIMARY KEY,
            call_id             UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            elder_id            UUID NOT NULL REFERENCES elders(id),
            requested_time_text TEXT NOT NULL,
            requested_at        TIMESTAMPTZ,
            notes               TEXT,
            status              TEXT NOT NULL DEFAULT 'open',
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_callback_requests_elder ON callback_requests(elder_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_callback_requests_status ON callback_requests(status, created_at DESC)"
    )

    # sms_messages: queued outbound texts, flushed post-call (spec §6.4).
    op.execute(
        """
        CREATE TABLE sms_messages (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            call_id           UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            elder_id          UUID NOT NULL REFERENCES elders(id),
            to_number         TEXT NOT NULL,
            template_key      TEXT NOT NULL,
            body              TEXT NOT NULL,
            status            TEXT NOT NULL DEFAULT 'pending',
            telnyx_message_id TEXT UNIQUE,
            error             JSONB,
            sent_at           TIMESTAMPTZ,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_sms_messages_call ON sms_messages(call_id, status)")
    op.execute("CREATE INDEX idx_sms_messages_status ON sms_messages(status, created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sms_messages")
    op.execute("DROP TABLE IF EXISTS callback_requests")
    op.execute("DROP TABLE IF EXISTS follow_up_flags")
