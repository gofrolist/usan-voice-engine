"""Migration 0033 adds the P2 identity columns + the memberships table
(P2 plan Unit A, Task A2 — design 2026-06-16-tenancy-p2-identity-rbac).

`admin_users` gains `is_super_admin`, `status`, `last_active_org_id` and loses
`role` (the per-person role moves onto the per-org `memberships` join). The
`memberships` table is global/non-RLS with a composite PK `(email,
organization_id)` and an `admin_role` enum role column.

Runs against the testcontainers Postgres that conftest's `database_url` already
migrated to head, so this asserts the migrated end-state and that the
`memberships` table accepts a usan membership. The role-backfill itself is
exercised by the alembic round-trip command in Step 4.

Uses the async (asyncpg) engine the rest of the migration suite uses — this env
has no sync Postgres driver installed (conftest's "asyncpg fallback" note), so a
sync ``sa.create_engine(postgresql://...)`` would raise ModuleNotFoundError
rather than exercise the schema.
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def _roundtrip(async_url: str) -> tuple[set[str], str]:
    engine = create_async_engine(async_url, poolclass=NullPool)
    try:
        async with engine.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO admin_users (email, is_super_admin, status, added_by) "
                    "VALUES ('legacy@x.com', false, 'active', 'seed') "
                    "ON CONFLICT (email) DO NOTHING"
                )
            )
            await c.execute(
                text(
                    "INSERT INTO memberships (email, organization_id, role, added_by) "
                    "SELECT 'legacy@x.com', id, CAST('viewer' AS admin_role), 'seed' "
                    "FROM organizations WHERE slug='usan' ON CONFLICT DO NOTHING"
                )
            )
        async with engine.connect() as c:
            rows = await c.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='admin_users'"
                )
            )
            cols = {r[0] for r in rows}
            role = (
                await c.execute(text("SELECT role FROM memberships WHERE email='legacy@x.com'"))
            ).scalar_one()
        # Clean up so this module does not strand rows for later TRUNCATE-based tests.
        async with engine.begin() as c:
            await c.execute(text("DELETE FROM memberships WHERE email='legacy@x.com'"))
            await c.execute(text("DELETE FROM admin_users WHERE email='legacy@x.com'"))
        return cols, role
    finally:
        await engine.dispose()


def test_0033_creates_memberships_and_migrates_role(database_url):
    async_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    cols, role = asyncio.run(_roundtrip(async_url))
    assert {"is_super_admin", "status", "last_active_org_id"} <= cols
    assert "role" not in cols
    assert role == "viewer"
