"""medication_reminders (US3 / Clara Care Parity 002)

A pending re-ask for a medication the elder reported NOT taken. The agent re-asks on
the next call (surfaced via the ``pending_med_reasks`` builtin); a confirmation clears
it, and after a cap of repeated not-taken reports it is ``capped`` and a routine
follow-up flag is opened so Clara stops nagging. Additive; no existing data is touched.

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-14

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # BigInteger PK (high-volume child rows). status is Text+CHECK so the set can widen
    # without an ORM recompile (mirrors family_tasks). opened_/cleared_call_id record
    # which call opened the re-ask and which one confirmed it. No ondelete on elder_id —
    # a reminder's context outlives an elder row change (mirrors follow_up_flags).
    op.execute(
        "CREATE TABLE medication_reminders ("
        "id BIGSERIAL PRIMARY KEY, "
        "elder_id UUID NOT NULL REFERENCES elders(id), "
        "medication_name TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending', "
        "attempt_count SMALLINT NOT NULL DEFAULT 0, "
        "next_reminder_at TIMESTAMPTZ, "
        "opened_call_id UUID REFERENCES calls(id), "
        "cleared_call_id UUID REFERENCES calls(id), "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "CONSTRAINT ck_medication_reminders_status CHECK "
        "(status IN ('pending', 'cleared', 'capped'))"
        ")"
    )
    # At most one PENDING re-ask per (elder, medication): a repeated not-taken report
    # refreshes the same row instead of duplicating. cleared/capped rows are unconstrained
    # so the history of past re-ask cycles is retained.
    op.execute(
        "CREATE UNIQUE INDEX uq_medication_reminders_pending "
        "ON medication_reminders(elder_id, medication_name) WHERE status = 'pending'"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS medication_reminders")
