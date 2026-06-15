"""family_contacts + family_tasks (US2 / Clara Care Parity 002)

A family_contact is a person linked to an elder who can send tasks and receive
alerts/reports. A family_task is a short instruction (e.g. "remind mom to drink
water") conveyed on the next call then closed. Both are additive; no existing data
is touched.

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-14

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # family_contacts: no ondelete on elder_id — a contact's context outlives an elder
    # row change (mirrors follow_up_flags). phone_e164 is NOT globally unique (one number
    # may relate to >1 elder), so it is indexed, not constrained.
    op.execute(
        "CREATE TABLE family_contacts ("
        "id UUID PRIMARY KEY DEFAULT gen_random_uuid(), "
        "elder_id UUID NOT NULL REFERENCES elders(id), "
        "name TEXT NOT NULL, "
        "phone_e164 TEXT NOT NULL, "
        "relationship TEXT, "
        "alert_prefs JSONB NOT NULL DEFAULT '{}', "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()"
        ")"
    )
    op.execute("CREATE INDEX idx_family_contacts_phone ON family_contacts(phone_e164)")
    op.execute("CREATE INDEX idx_family_contacts_elder ON family_contacts(elder_id)")

    # family_tasks: BigInteger PK (high-volume child rows). status is Text+CHECK so the
    # set can widen without an ORM recompile. delivered_call_id records which call
    # conveyed the task. status audit fields mirror follow_up_flags.
    op.execute(
        "CREATE TABLE family_tasks ("
        "id BIGSERIAL PRIMARY KEY, "
        "elder_id UUID NOT NULL REFERENCES elders(id), "
        "family_contact_id UUID REFERENCES family_contacts(id), "
        "message TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'open', "
        "needs_safety_review BOOLEAN NOT NULL DEFAULT false, "
        "delivered_call_id UUID REFERENCES calls(id), "
        # Telnyx inbound message id (idempotency key); NULL for operator-entered tasks.
        # UNIQUE allows many NULLs in PG, so only inbound rows are deduplicated.
        "inbound_message_id TEXT UNIQUE, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
        "status_updated_at TIMESTAMPTZ, "
        "status_updated_by TEXT, "
        "CONSTRAINT ck_family_tasks_status CHECK "
        "(status IN ('open', 'delivered', 'closed', 'needs_review'))"
        ")"
    )
    # Injection lookup: open, non-safety-review tasks for an elder (open_family_tasks).
    op.execute("CREATE INDEX idx_family_tasks_elder_status ON family_tasks(elder_id, status)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS family_tasks")
    op.execute("DROP TABLE IF EXISTS family_contacts")
