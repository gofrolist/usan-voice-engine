"""dnc_list: widen PK to (phone_e164, organization_id)

Revision ID: 0052
Revises: 0051
Create Date: 2026-07-02

The tenancy fanout (0032) added organization_id + RLS to dnc_list but left the
primary key on phone_e164 alone. Under RLS, org B cannot see org A's row for a
number, so an upsert-check misses it and the INSERT collides on the sole-phone PK
(500). Widen the PK so the same number can be independently suppressed per org.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # organization_id is already NOT NULL (0032), so it is a valid PK member.
    op.execute("ALTER TABLE dnc_list DROP CONSTRAINT dnc_list_pkey")
    op.execute(
        "ALTER TABLE dnc_list ADD CONSTRAINT dnc_list_pkey "
        "PRIMARY KEY (phone_e164, organization_id)"
    )


def downgrade() -> None:
    # Reverting to a sole-phone PK requires uniqueness on phone_e164 alone. If more
    # than one org has suppressed the same number this will fail — which is correct,
    # since collapsing them would lose data.
    op.execute("ALTER TABLE dnc_list DROP CONSTRAINT dnc_list_pkey")
    op.execute("ALTER TABLE dnc_list ADD CONSTRAINT dnc_list_pkey PRIMARY KEY (phone_e164)")
