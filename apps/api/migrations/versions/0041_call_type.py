"""call_type: discriminator column on calls for the web-call surface.

Adds a native enum ``call_type`` (phone_call | web_call) and the ``calls.call_type``
column, NOT NULL with server_default 'phone_call' so the populated table backfills and
every existing phone path keeps working. The ``calls`` table already grants CRUD to
usan_app (column inherits it); the new enum TYPE needs no grant. Owner DDL — the deploy
runs alembic as the `usan` table owner before `compose up`.

Revision ID: 0041
Revises: 0040
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    call_type = postgresql.ENUM("phone_call", "web_call", name="call_type", create_type=False)
    call_type.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "calls",
        sa.Column("call_type", call_type, nullable=False, server_default="phone_call"),
    )


def downgrade() -> None:
    op.drop_column("calls", "call_type")
    postgresql.ENUM(name="call_type", create_type=False).drop(op.get_bind(), checkfirst=True)
