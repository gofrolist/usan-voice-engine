"""follow_up_flags crisis columns (US1 / Clara Care Parity 002)

A crisis escalation is a ``severity='urgent'`` follow_up_flags row with these columns
populated. detection_source becomes 'both' when the LLM and the deterministic safety
net independently flag the same (call_id, crisis_category). The partial unique index
makes raise_crisis idempotent per (call_id, category) within a call.

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-14

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE follow_up_flags ADD COLUMN crisis_category TEXT")
    op.execute("ALTER TABLE follow_up_flags ADD COLUMN detection_source TEXT")
    op.execute("ALTER TABLE follow_up_flags ADD COLUMN resource_offered TEXT")
    op.execute(
        "ALTER TABLE follow_up_flags ADD COLUMN family_notified BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "ALTER TABLE follow_up_flags ADD CONSTRAINT ck_followup_crisis_category "
        "CHECK (crisis_category IS NULL OR crisis_category IN "
        "('suicidal', 'medical', 'abuse', 'confusion', 'overdose'))"
    )
    op.execute(
        "ALTER TABLE follow_up_flags ADD CONSTRAINT ck_followup_detection_source "
        "CHECK (detection_source IS NULL OR detection_source IN ('llm', 'safety_net', 'both'))"
    )
    # Idempotency: one crisis flag per (call_id, category) within a call. Partial so it
    # only constrains crisis rows; ordinary follow-up flags (crisis_category NULL) are
    # unaffected and may repeat per call.
    op.execute(
        "CREATE UNIQUE INDEX uq_followup_crisis ON follow_up_flags(call_id, crisis_category) "
        "WHERE crisis_category IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_followup_crisis")
    op.execute("ALTER TABLE follow_up_flags DROP CONSTRAINT IF EXISTS ck_followup_detection_source")
    op.execute("ALTER TABLE follow_up_flags DROP CONSTRAINT IF EXISTS ck_followup_crisis_category")
    op.execute("ALTER TABLE follow_up_flags DROP COLUMN IF EXISTS family_notified")
    op.execute("ALTER TABLE follow_up_flags DROP COLUMN IF EXISTS resource_offered")
    op.execute("ALTER TABLE follow_up_flags DROP COLUMN IF EXISTS detection_source")
    op.execute("ALTER TABLE follow_up_flags DROP COLUMN IF EXISTS crisis_category")
