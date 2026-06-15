"""notification substrate: sms_messages call_id nullable + kind + dedupe_key

Foundational for Clara Care Parity (002): turns sms_messages into a channel for
non-call notifications (family alerts/reports, opt-out acks) alongside the existing
in-call texts. call_id becomes nullable; a `kind` discriminator and an idempotent
`dedupe_key` are added; template_key becomes nullable (system templates carry no key).

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-14

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Non-call notifications have no owning call; the FK + ON DELETE CASCADE stay for
    # in-call rows. template_key is NULL for system-template notifications.
    op.execute("ALTER TABLE sms_messages ALTER COLUMN call_id DROP NOT NULL")
    op.execute("ALTER TABLE sms_messages ALTER COLUMN template_key DROP NOT NULL")

    # kind discriminator. Default 'in_call' backfills every existing row, so the new
    # NOT NULL holds without a data migration.
    op.execute("ALTER TABLE sms_messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'in_call'")
    op.execute(
        "ALTER TABLE sms_messages ADD CONSTRAINT ck_sms_messages_kind "
        "CHECK (kind IN ('in_call', 'family_alert', 'family_report', 'opt_out_ack'))"
    )

    # dedupe_key makes family notifications idempotent (e.g. 'crisis:{flag_id}',
    # 'missed:{call_id}'). A plain UNIQUE index is "unique-where-not-null" in Postgres:
    # NULLs are distinct, so the many in-call rows (dedupe_key IS NULL) never collide.
    op.execute("ALTER TABLE sms_messages ADD COLUMN dedupe_key TEXT")
    op.execute("CREATE UNIQUE INDEX uq_sms_messages_dedupe_key ON sms_messages(dedupe_key)")

    # The notification outbox poller claims status='pending' rows with no owning call;
    # this partial index serves exactly that predicate.
    op.execute(
        "CREATE INDEX idx_sms_notifications ON sms_messages(status, created_at) "
        "WHERE call_id IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_sms_notifications")
    op.execute("DROP INDEX IF EXISTS uq_sms_messages_dedupe_key")
    op.execute("ALTER TABLE sms_messages DROP COLUMN IF EXISTS dedupe_key")
    op.execute("ALTER TABLE sms_messages DROP CONSTRAINT IF EXISTS ck_sms_messages_kind")
    op.execute("ALTER TABLE sms_messages DROP COLUMN IF EXISTS kind")
    # Best-effort restore of the pre-0017 NOT NULLs (downgrades are not run in prod and
    # would fail if any non-call rows exist; acceptable for a backward migration).
    op.execute("ALTER TABLE sms_messages ALTER COLUMN template_key SET NOT NULL")
    op.execute("ALTER TABLE sms_messages ALTER COLUMN call_id SET NOT NULL")
