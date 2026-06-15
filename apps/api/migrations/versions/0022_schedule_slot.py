"""call_schedules.slot — per-slot (morning|evening) schedules (US5 / Clara Care Parity 002)

Lets an elder have an independent morning AND evening daily wellness call. Adds a
``slot`` column (morning|evening, default 'morning' so existing rows backfill in
place) and relaxes the one-schedule-per-elder ``UNIQUE (elder_id)`` to
``UNIQUE (elder_id, slot)``. Each slot stays its own row with its own
window/days/enabled flag and its own ``sched:`` idempotency key, so the phase-3
materializer already iterates them independently — no orchestrator change.
Additive + an in-place backfill; no existing data is dropped.

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-15

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # slot defaults to 'morning': every pre-existing schedule keeps dialing as the
    # morning slot, and the CHECK pins the closed set (widen via migration, like the
    # other ck_call_schedules_* constraints).
    op.execute(
        "ALTER TABLE call_schedules "
        "ADD COLUMN slot TEXT NOT NULL DEFAULT 'morning' "
        "CONSTRAINT ck_call_schedules_slot CHECK (slot IN ('morning', 'evening'))"
    )
    # One schedule per (elder, slot): drop the inline single-column UNIQUE (Postgres
    # default name <table>_<column>_key from 0012) and add the composite.
    op.execute("ALTER TABLE call_schedules DROP CONSTRAINT call_schedules_elder_id_key")
    op.execute(
        "ALTER TABLE call_schedules "
        "ADD CONSTRAINT uq_call_schedules_elder_slot UNIQUE (elder_id, slot)"
    )


def downgrade() -> None:
    # Reversible only while no elder has two slots; the restored single-column UNIQUE
    # would otherwise reject the rollback (acceptable — forward-only in practice).
    op.execute("ALTER TABLE call_schedules DROP CONSTRAINT uq_call_schedules_elder_slot")
    op.execute(
        "ALTER TABLE call_schedules ADD CONSTRAINT call_schedules_elder_id_key UNIQUE (elder_id)"
    )
    op.execute("ALTER TABLE call_schedules DROP COLUMN slot")
