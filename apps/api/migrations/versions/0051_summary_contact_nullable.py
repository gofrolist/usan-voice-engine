"""conversation_summaries.contact_id: allow NULL (Phase 7 slice 2, rerun-call-analysis).

A compat rerun of a contact-less web call persists a summary row with contact_id NULL.
The next-call built-ins read summaries via get_latest(contact_id=...), so NULL rows can
never feed them. Owner-DDL (ALTER TABLE runs as the usan owner on deploy); additive/inert.

Revision ID: 0051
Revises: 0050
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "conversation_summaries",
        "contact_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "conversation_summaries",
        "contact_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
