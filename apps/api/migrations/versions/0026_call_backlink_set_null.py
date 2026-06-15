"""call back-link FKs: ON DELETE SET NULL for consistency (review L9)

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-15

medication_reminders.opened_call_id / cleared_call_id and family_tasks.delivered_call_id
were created with a bare ``REFERENCES calls(id)`` (defaulting to NO ACTION / RESTRICT),
while every sibling "optional back-link to the call that touched this row" column
(conversation_summaries, wellbeing_survey_results, activity_history, callback_requests,
family_reports) uses ON DELETE SET NULL — "keep the record, drop the back-link". Align the
three outliers so a future/manual call delete behaves uniformly instead of being blocked by
a foreign-key violation on these tables only. Latent today (no call-delete path exists).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column) -> the Postgres auto-named FK constraint (<table>_<column>_fkey).
_FKS = (
    ("medication_reminders", "opened_call_id"),
    ("medication_reminders", "cleared_call_id"),
    ("family_tasks", "delivered_call_id"),
)


def upgrade() -> None:
    for table, col in _FKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {table}_{col}_fkey")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_{col}_fkey "
            f"FOREIGN KEY ({col}) REFERENCES calls(id) ON DELETE SET NULL"
        )


def downgrade() -> None:
    for table, col in _FKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {table}_{col}_fkey")
        op.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_{col}_fkey "
            f"FOREIGN KEY ({col}) REFERENCES calls(id)"
        )
