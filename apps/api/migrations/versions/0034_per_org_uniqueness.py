"""per-org uniqueness: composite UNIQUE(natural_key, organization_id)

P2 plan Unit D, Task D1 (design 2026-06-16-tenancy-p2-identity-rbac).

The P1-era single-column uniques on tenant-scoped natural keys were globally
unique, so two orgs could never share (say) a phone number. Multi-tenancy makes
each natural key unique *within an org*: drop the single-column unique and add a
composite ``UNIQUE(column, organization_id)``. Postgres treats NULLs as
distinct, so the nullable keys (``external_id``, ``idempotency_key``,
``dedupe_key``) keep today's "many NULLs per org" behavior.

Constraint names below were confirmed against the live schema via
``SELECT conname FROM pg_constraint`` — the P1-era auto-named uniques are NOT all
``<table>_<column>_key``: ``contacts`` kept its pre-rename ``elders_*`` names
(migration 0027 renamed the table but not its constraints), and
``sms_messages.dedupe_key`` is a UNIQUE *index* (``uq_sms_messages_dedupe_key``,
created in 0017), not a table constraint — so it is handled with
``drop_index``/``create_index`` rather than ``drop_constraint``.

EXCLUDED (provider-global / UUID — no cross-org collision, left global):
``sms_messages.telnyx_message_id``, ``family_tasks.inbound_message_id``,
``conversation_summaries`` (unique is on the ``call_id`` UUID), and
``organizations.slug``.

Revision ID: 0034
Revises: 0033
Create Date: 2026-06-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column, existing single-column-unique constraint name). The P1-era
# auto-named uniques; ``contacts`` kept its pre-rename ``elders_*`` names.
_PER_ORG_CONSTRAINTS: list[tuple[str, str, str]] = [
    ("contacts", "phone_e164", "elders_phone_e164_key"),
    ("contacts", "external_id", "elders_external_id_key"),
    ("agent_profiles", "name", "agent_profiles_name_key"),
    ("custom_variables", "name", "custom_variables_name_key"),
    ("calls", "idempotency_key", "calls_idempotency_key_key"),
    ("call_batches", "idempotency_key", "call_batches_idempotency_key_key"),
]

# sms_messages.dedupe_key uniqueness is a UNIQUE INDEX (0017), not a constraint.
_SMS_TABLE = "sms_messages"
_SMS_COL = "dedupe_key"
_SMS_OLD_INDEX = "uq_sms_messages_dedupe_key"
_SMS_NEW_INDEX = "uq_sms_messages_dedupe_key_org"


def _uq_name(table: str, column: str) -> str:
    return f"uq_{table}_{column}_org"


def upgrade() -> None:
    for table, column, old_name in _PER_ORG_CONSTRAINTS:
        op.drop_constraint(old_name, table, type_="unique")
        op.create_unique_constraint(_uq_name(table, column), table, [column, "organization_id"])

    # dedupe_key: swap the single-column UNIQUE index for a composite one.
    op.drop_index(_SMS_OLD_INDEX, table_name=_SMS_TABLE)
    op.create_index(_SMS_NEW_INDEX, _SMS_TABLE, [_SMS_COL, "organization_id"], unique=True)


def downgrade() -> None:
    op.drop_index(_SMS_NEW_INDEX, table_name=_SMS_TABLE)
    op.create_index(_SMS_OLD_INDEX, _SMS_TABLE, [_SMS_COL], unique=True)

    for table, column, old_name in _PER_ORG_CONSTRAINTS:
        op.drop_constraint(_uq_name(table, column), table, type_="unique")
        op.create_unique_constraint(old_name, table, [column])
