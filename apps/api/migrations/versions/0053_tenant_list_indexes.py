"""Composite (organization_id, sort, id) indexes for hot tenant list queries

Revision ID: 0053
Revises: 0052
Create Date: 2026-07-02

RLS injects `organization_id = current_setting(...)` as an implicit predicate on every
query, so a single-column sort index (e.g. idx_calls_created) is walked across ALL orgs
with the org filter applied per-row. As more orgs onboard, a small tenant's "recent N"
page walks far more index entries than needed. These composite indexes let Postgres seek
straight to the org's slice in sort order. Also indexes contacts.name (previously unindexed,
forcing a full sort of the org's roster on every admin list page).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (index name, table, column DDL). Raw column DDL so DESC matches the list endpoints' ORDER BY.
_INDEXES = [
    ("ix_calls_org_created_id", "calls", "organization_id, created_at DESC, id DESC"),
    (
        "ix_chat_sessions_org_started_id",
        "chat_sessions",
        "organization_id, started_at DESC, id DESC",
    ),
    (
        "ix_phone_numbers_org_created_id",
        "phone_numbers",
        "organization_id, created_at DESC, id DESC",
    ),
    ("ix_contacts_org_name_id", "contacts", "organization_id, name, id"),
]


def upgrade() -> None:
    for name, table, cols in _INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})")


def downgrade() -> None:
    for name, _table, _cols in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
