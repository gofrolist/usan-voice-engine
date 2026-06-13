"""agent_profiles.draft_revision: optimistic-concurrency token

Revision ID: 0016
Revises: 0015
Create Date: 2026-06-13

Additive, backward-compatible: a monotonic integer bumped by every row-mutating
path (update_draft / publish / rollback). The PUT /draft body carries the
loaded value as ``expected_revision``; a guarded conditional UPDATE that matches
0 rows means the draft changed since it was loaded -> HTTP 409 (FR-032 / SC-011).
Existing rows default to 1; no backfill.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent_profiles ADD COLUMN draft_revision INTEGER NOT NULL DEFAULT 1")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_profiles DROP COLUMN IF EXISTS draft_revision")
