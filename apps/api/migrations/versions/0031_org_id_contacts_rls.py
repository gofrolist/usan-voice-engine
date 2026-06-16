"""organization_id + RLS on contacts (pilot)

Revision ID: 0031
Revises: 0030
Create Date: 2026-06-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_ORG = "(SELECT id FROM organizations WHERE slug = 'usan')"
# Column DEFAULT for organization_id. Postgres forbids a *subquery* in a DEFAULT
# expression ("cannot use subquery in DEFAULT expression"), so the default-org lookup
# is wrapped in the default_org_id() function (created below) and called here. The
# COALESCE pulls the org from the request context first; when context is unset
# (superuser test seeds) it falls back to the default org. Reused verbatim by 0032.
_ORG_DEFAULT_EXPR = "COALESCE(current_setting('app.current_org', true)::uuid, default_org_id())"


def upgrade() -> None:
    # Encapsulate the default-org lookup in a function so the column DEFAULT can call it
    # (a bare subquery is rejected in a DEFAULT expression). STABLE: one snapshot per
    # statement is fine. Granted to PUBLIC by default; usan_app can call it.
    op.execute(
        "CREATE OR REPLACE FUNCTION default_org_id() RETURNS uuid LANGUAGE sql STABLE AS "
        "$$ SELECT id FROM organizations WHERE slug = 'usan' $$"
    )
    op.add_column("contacts", sa.Column("organization_id", sa.Uuid(), nullable=True))
    # Backfill from a hardcoded constant (no user input); S608 is a false positive.
    backfill = f"UPDATE contacts SET organization_id = {_DEFAULT_ORG} WHERE organization_id IS NULL"  # noqa: S608
    op.execute(backfill)
    op.alter_column("contacts", "organization_id", nullable=False)
    # Future inserts get the org from the tenant context (COALESCE fallback = default
    # org, so superuser seeds with no context still succeed). Set AFTER NOT NULL so the
    # ADD COLUMN above stays a fast metadata-only change (no per-row default eval).
    op.execute(f"ALTER TABLE contacts ALTER COLUMN organization_id SET DEFAULT {_ORG_DEFAULT_EXPR}")
    op.create_foreign_key(
        "fk_contacts_organization", "contacts", "organizations", ["organization_id"], ["id"]
    )
    op.create_index("ix_contacts_organization_id", "contacts", ["organization_id"])
    op.execute("ALTER TABLE contacts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE contacts FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON contacts
          USING (organization_id = current_setting('app.current_org', true)::uuid)
          WITH CHECK (organization_id = current_setting('app.current_org', true)::uuid)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON contacts")
    op.execute("ALTER TABLE contacts NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE contacts DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_contacts_organization_id", table_name="contacts")
    op.drop_constraint("fk_contacts_organization", "contacts", type_="foreignkey")
    op.drop_column("contacts", "organization_id")
    # The function is shared with 0032's tables; only drop it here (0031 is the floor
    # that created it). 0032's downgrade runs first, so by the time we reach 0030 no
    # tenant table references it anymore.
    op.execute("DROP FUNCTION IF EXISTS default_org_id()")
