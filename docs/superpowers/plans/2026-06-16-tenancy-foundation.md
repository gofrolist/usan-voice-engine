# P1 — Multi-Tenant Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make tenant isolation a database-enforced invariant — every PHI/config row carries `organization_id`, Postgres RLS fails closed when no tenant context is set, and the app connects as a non-superuser role so RLS actually applies — while production behavior is unchanged (one implicit default org).

**Architecture:** A non-superuser `usan_app` login role (RLS applies to it; superusers bypass RLS, which is why this is Task 1). An `organizations` table + `organization_id` on all 28 tenant-owned tables. A `tenant_context` module sets `SET LOCAL app.current_org` per transaction; in P1 it always resolves the seeded default org, so every existing caller is transparently scoped to one org. Uniform fail-closed RLS policies (`USING`/`WITH CHECK` on `current_setting('app.current_org', true)`). Proven on one pilot table first, then fanned out.

**Tech Stack:** FastAPI + SQLAlchemy 2 async (asyncpg) + Alembic (apps/api, Python 3.14, uv); Postgres 18 (pgvector image in tests via testcontainers; Cloud SQL in prod). Migration head is **0028**.

**Spec:** `docs/superpowers/specs/2026-06-16-tenancy-foundation-design.md`

**Branch:** `feat/tenancy-foundation` (off `origin/main`, already created).

**Working dir:** all API commands run from `apps/api`. Run pytest with `uv run pytest`, mypy with `uv run mypy`, migrations with `uv run alembic ...`.

### The 28 tenant-owned tables (canonical list — used verbatim by the migrations)
```
contacts, dnc_list, calls, transcripts, wellness_logs, medication_logs,
medication_reminders, personal_facts, conversation_summaries,
wellbeing_survey_results, activity_history, turn_metrics, call_metrics,
agent_profiles, agent_profile_versions, call_schedules, call_batches,
call_batch_targets, webhook_endpoints, webhook_deliveries, custom_variables,
admin_audit_log, follow_up_flags, callback_requests, sms_messages,
family_contacts, family_tasks, family_reports
```
`organizations` (new) and `admin_users` stay global (admin_users gets `organization_id` in P2).

### File structure
- `apps/api/migrations/versions/0029_app_role.py` — non-superuser `usan_app` role + grants.
- `apps/api/migrations/versions/0030_organizations.py` — `organizations` table + seed default org.
- `apps/api/migrations/versions/0031_org_id_contacts_rls.py` — pilot: `organization_id` + RLS on `contacts`.
- `apps/api/migrations/versions/0032_org_id_rls_fanout.py` — remaining 27 tables (DRY loop).
- `apps/api/src/usan_api/db/models.py` — `Organization` model + `TenantScoped` mixin on the 28 models.
- `apps/api/src/usan_api/tenant_context.py` — resolve default org + `SET LOCAL` helper (new).
- `apps/api/src/usan_api/db/session.py` — `get_db` sets tenant context per request.
- `apps/api/src/usan_api/repositories/organizations.py` — org lookup/seed (new).
- `apps/api/tests/test_rls_isolation.py` — the isolation test suite (new).
- `apps/api/tests/conftest.py` — set `usan_app` login password; app-under-test + isolation engine connect as `usan_app`.
- `infra/terraform/database.tf` + `infra/.env.prod.example` — prod `usan_app` user + DATABASE_URL (Task 7).

---

## Task 1: Non-superuser `usan_app` role + grants + run app-under-test as it

**Why first:** RLS does nothing for a superuser. Tests connect as `usan` (a real superuser in the pgvector image) and prod's `usan` has `cloudsqlsuperuser`. The app must connect as a non-superuser, non-`BYPASSRLS` role or the entire spec is inert.

**Files:**
- Create: `apps/api/migrations/versions/0029_app_role.py`
- Modify: `apps/api/tests/conftest.py`
- Test: `apps/api/tests/test_app_role.py` (create)

- [ ] **Step 1: Write the migration** (idempotent role, GRANTs, default privileges — mirrors `0009_grafana_ro_role.py`)

```python
"""usan_app: non-superuser app login role subject to RLS

Revision ID: 0029
Revises: 0028
Create Date: 2026-06-16

The app connects as this role so Row-Level Security (added in 0031/0032) actually
applies — superusers and BYPASSRLS roles ignore RLS. No password here (committed
migrations carry no secrets); the login password is provisioned out-of-band by
Terraform in prod (google_sql_user.usan_app) and by the test harness in CI.
ALTER DEFAULT PRIVILEGES (FOR ROLE usan, the migration runner/owner) ensures
tables created by later migrations are auto-granted to usan_app.
"""

from collections.abc import Sequence
from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'usan_app') THEN
                CREATE ROLE usan_app NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE LOGIN;
            END IF;
        END
        $$
        """
    )
    op.execute("GRANT CONNECT ON DATABASE usan TO usan_app")
    op.execute("GRANT USAGE ON SCHEMA public TO usan_app")
    # CRUD on all current tables + sequence usage (serial PKs).
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO usan_app"
    )
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO usan_app")
    # Future tables/sequences created by the migration runner (role usan) auto-granted.
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE usan IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO usan_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE usan IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO usan_app"
    )


def downgrade() -> None:
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE usan IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM usan_app"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE usan IN SCHEMA public "
        "REVOKE USAGE, SELECT ON SEQUENCES FROM usan_app"
    )
    op.execute("REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM usan_app")
    op.execute("REVOKE ALL ON ALL TABLES IN SCHEMA public FROM usan_app")
    op.execute("REVOKE USAGE ON SCHEMA public FROM usan_app")
    op.execute("REVOKE CONNECT ON DATABASE usan FROM usan_app")
    op.execute("DROP ROLE IF EXISTS usan_app")
```

- [ ] **Step 2: Make the test harness set a login password for `usan_app` and connect the app-under-test as it**

First verify which client driver is available: `cd apps/api && uv run python -c "import psycopg; print('psycopg ok')"`. If that prints ok, use the psycopg form below; if it errors, use the asyncpg fallback noted after.

In `apps/api/tests/conftest.py`, in the `database_url` fixture, right after the `alembic upgrade head` subprocess succeeds (before `yield url`), add:

```python
        # usan_app is created by migration 0029 with LOGIN but no password (secrets
        # never live in migrations). Give it a known test password so the
        # app-under-test (and the isolation suite) connect as the RLS-subject role.
        import psycopg  # psycopg v3
        with psycopg.connect(url) as conn:
            conn.execute("ALTER ROLE usan_app WITH LOGIN PASSWORD 'usan_app'")
            conn.commit()
        os.environ["APP_DATABASE_URL"] = f"postgresql://usan_app:usan_app@{host}:{port}/usan"
        yield url
```
asyncpg fallback (if psycopg is unavailable) — define a module-level helper mirroring `_seed_admin_user_async` and call it via `asyncio.run`:
```python
async def _set_app_role_password(super_url: str) -> None:
    eng = create_async_engine(super_url.replace("postgresql://", "postgresql+asyncpg://", 1),
                              poolclass=NullPool)
    try:
        async with eng.begin() as conn:
            await conn.execute(text("ALTER ROLE usan_app WITH LOGIN PASSWORD 'usan_app'"))
    finally:
        await eng.dispose()
# ...then in the fixture: asyncio.run(_set_app_role_password(url)) before setting APP_DATABASE_URL.
```

Then in the **`client`** fixture (and **`sso_client`**), change the line that sets the app's DB env so the app connects as `usan_app`:
```python
    monkeypatch.setenv("DATABASE_URL", os.environ["APP_DATABASE_URL"])
```
Leave `async_database_url` and the `_seed_*` helpers connecting as `usan` — seeding runs as superuser (bypasses RLS), which is intended for cross-org test setup.

- [ ] **Step 3: Write the role test**

Create `apps/api/tests/test_app_role.py`:
```python
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def test_usan_app_is_not_superuser_and_not_bypassrls(async_database_url):
    async def check() -> tuple[bool, bool]:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT rolsuper, rolbypassrls FROM pg_roles "
                            "WHERE rolname = 'usan_app'"
                        )
                    )
                ).one()
            return bool(row[0]), bool(row[1])
        finally:
            await engine.dispose()

    rolsuper, rolbypassrls = asyncio.run(check())
    assert rolsuper is False
    assert rolbypassrls is False
```

Run: `cd apps/api && uv run pytest tests/test_app_role.py -v`
Expected: PASS. If the `database_url` fixture errors, the migration or the `ALTER ROLE` step is wrong — fix before proceeding.

- [ ] **Step 4: Run the existing suite (app now connects as `usan_app`; no RLS yet)**

Run: `cd apps/api && uv run pytest -q`
Expected: PASS. (No RLS exists yet, so `usan_app` with full grants behaves like `usan`. A failure here means a missing GRANT — fix the migration.)

- [ ] **Step 5: Commit**

```bash
git add apps/api/migrations/versions/0029_app_role.py apps/api/tests/conftest.py apps/api/tests/test_app_role.py
git commit -m "feat(api): non-superuser usan_app role; run app under it (RLS prerequisite)"
```

---

## Task 2: `organizations` table + model + repo + seeded default org

**Files:**
- Create: `apps/api/migrations/versions/0030_organizations.py`
- Modify: `apps/api/src/usan_api/db/models.py`
- Create: `apps/api/src/usan_api/repositories/organizations.py`
- Modify: `apps/api/src/usan_api/settings.py` (add `default_org_slug`)
- Test: `apps/api/tests/test_organizations_repo.py` (create)

- [ ] **Step 1: Write the migration (table + seed default org)**

```python
"""organizations table + seeded default org

Revision ID: 0030
Revises: 0029
Create Date: 2026-06-16
"""
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    op.execute(
        "INSERT INTO organizations (name, slug) VALUES ('USAN Retirement', 'usan')"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON organizations TO usan_app")


def downgrade() -> None:
    op.drop_table("organizations")
```

- [ ] **Step 2: Add the `Organization` model and `TenantScoped` mixin to `db/models.py`**

Ensure `import uuid`, `from sqlalchemy import ForeignKey`, and `from sqlalchemy import text` are present (add if missing). Use the file's existing `Mapped`/`mapped_column`/`Text`/`DateTime`/`func` imports verbatim. Add:

```python
class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class TenantScoped:
    """Mixin adding the tenant FK. Applied to every tenant-owned model in Tasks 4-5.

    The column is added to the DB by migrations 0031/0032; this mixin keeps the ORM
    mapping in sync. organization_id is filled by a DB column DEFAULT sourced from the
    tenant context on INSERT (see the migrations' SET DEFAULT) — so repositories never
    set it and existing insert code is unchanged — and RLS WITH CHECK rejects any
    cross-org mismatch. The server_default below mirrors that DDL so SQLAlchemy omits
    the column from INSERTs and reads it back via RETURNING.
    """

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id"),
        nullable=False,
        index=True,
        server_default=text(
            "COALESCE(current_setting('app.current_org', true)::uuid,"
            " (SELECT id FROM organizations WHERE slug = 'usan'))"
        ),
    )
```

> **Why a DB default (deviation from spec §3.4's "repository stamps it"):** the column DEFAULT pulls the org from the request context (`current_setting`), so the app role's inserts land in its context org automatically and *raw-SQL* inserts are covered too — no repo or test-seed-helper churn. The `COALESCE` fallback to the default org lets the superuser test-seed helpers (which insert with no context and bypass RLS) succeed; the app role always has context set, so for it the `COALESCE` short-circuits to the context org and strict `WITH CHECK` still blocks any cross-org insert. **P2 follow-up:** once multi-org inserts are real, reconsider dropping the `COALESCE` fallback so a no-context insert fails instead of silently landing in the default org.

- [ ] **Step 3: Add `default_org_slug` setting**

In `apps/api/src/usan_api/settings.py`, alongside the other fields:
```python
    default_org_slug: str = Field(default="usan", alias="DEFAULT_ORG_SLUG")
```

- [ ] **Step 4: Write the repo test (TDD)**

Create `apps/api/tests/test_organizations_repo.py`:
```python
import asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories.organizations import get_org_by_slug


def test_default_org_seeded(async_database_url):
    async def run():
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                return await get_org_by_slug(s, "usan")
        finally:
            await engine.dispose()

    org = asyncio.run(run())
    assert org is not None
    assert org.slug == "usan"
    assert org.name == "USAN Retirement"
```
Run: `cd apps/api && uv run pytest tests/test_organizations_repo.py -v` → FAIL (no module).

- [ ] **Step 5: Implement the repo**

Create `apps/api/src/usan_api/repositories/organizations.py`:
```python
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import Organization


async def get_org_by_slug(db: AsyncSession, slug: str) -> Organization | None:
    result = await db.execute(select(Organization).where(Organization.slug == slug))
    return result.scalar_one_or_none()


async def get_org(db: AsyncSession, org_id: uuid.UUID) -> Organization | None:
    return await db.get(Organization, org_id)
```
Run the test again → PASS.

- [ ] **Step 6: Confirm `organizations` is NOT in the test truncate list**

Verify `_TRUNCATE_ALL` in `conftest.py` does not contain `organizations` (it must survive between tests so the default org persists). It currently doesn't — do not add it.

- [ ] **Step 7: Commit**

```bash
git add apps/api/migrations/versions/0030_organizations.py apps/api/src/usan_api/db/models.py apps/api/src/usan_api/settings.py apps/api/src/usan_api/repositories/organizations.py apps/api/tests/test_organizations_repo.py
git commit -m "feat(api): organizations table + model + repo + seeded default org"
```

---

## Task 3: Tenant-context module + per-request `SET LOCAL`

**Files:**
- Create: `apps/api/src/usan_api/tenant_context.py`
- Modify: `apps/api/src/usan_api/db/session.py`
- Test: `apps/api/tests/test_tenant_context.py` (create)

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_tenant_context.py`:
```python
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.tenant_context import resolve_default_org_id, set_tenant_context


def test_set_tenant_context_sets_guc(async_database_url):
    async def run() -> str | None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                org_id = await resolve_default_org_id(s)
                await set_tenant_context(s, org_id)
                got = (
                    await s.execute(text("SELECT current_setting('app.current_org', true)"))
                ).scalar_one()
            return got
        finally:
            await engine.dispose()

    val = asyncio.run(run())
    assert val  # a uuid string, not empty/None
```
Run: `cd apps/api && uv run pytest tests/test_tenant_context.py -v` → FAIL (no module).

- [ ] **Step 2: Implement `tenant_context.py`**

```python
"""Per-transaction tenant context for RLS.

set_tenant_context issues set_config('app.current_org', <uuid>, is_local=true) — the
parameterizable equivalent of SET LOCAL, transaction-scoped, so the value never leaks
across pooled connections. RLS policies read it via current_setting('app.current_org',
true); unset => NULL => zero rows (fail-closed).

In P1 the resolver always returns the single seeded default org, so every existing
caller is transparently scoped to one org and behavior is unchanged. P2 replaces the
resolver with "the authenticated user's org / act-as target".
"""
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.repositories.organizations import get_org_by_slug
from usan_api.settings import get_settings

_default_org_id: uuid.UUID | None = None


async def resolve_default_org_id(db: AsyncSession) -> uuid.UUID:
    """The seeded default org's id, cached after first lookup."""
    global _default_org_id
    if _default_org_id is None:
        org = await get_org_by_slug(db, get_settings().default_org_slug)
        if org is None:
            raise RuntimeError("default organization is not seeded")
        _default_org_id = org.id
    return _default_org_id


async def set_tenant_context(db: AsyncSession, org_id: uuid.UUID) -> None:
    # is_local=true => transaction-scoped, cleared at COMMIT/ROLLBACK; no cross-request leak.
    await db.execute(
        text("SELECT set_config('app.current_org', :org, true)"),
        {"org": str(org_id)},
    )


@asynccontextmanager
async def session_in_default_org() -> AsyncIterator[AsyncSession]:
    """A session with tenant context pre-set to the default org, for background workers.

    P1 single-org behavior. P2 will replace this with per-org iteration (Open Q1).
    """
    from usan_api.db.session import get_session_factory

    async with get_session_factory()() as session:
        org_id = await resolve_default_org_id(session)
        await set_tenant_context(session, org_id)
        yield session
```

- [ ] **Step 3: Run the test → PASS**

Run: `cd apps/api && uv run pytest tests/test_tenant_context.py -v`

- [ ] **Step 4: Wire `get_db` to set the default-org context per request**

In `apps/api/src/usan_api/db/session.py`, update `get_db`:
```python
async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Sets the tenant context (P1: the default org), then yields.

    Handlers commit explicitly; this only rolls back and closes. Context is set inside
    the session's transaction (set_config is_local=true) so RLS sees it and it's cleared
    when the transaction ends — no leak across pooled connections.
    """
    from usan_api.tenant_context import resolve_default_org_id, set_tenant_context

    async with get_session_factory()() as session:
        try:
            org_id = await resolve_default_org_id(session)
            await set_tenant_context(session, org_id)
            yield session
        except Exception:
            await session.rollback()
            raise
```
(Import inside the function to avoid a circular import between `session` and `tenant_context`.)

- [ ] **Step 5: Run the full suite (still no RLS; context now set everywhere)**

Run: `cd apps/api && uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/tenant_context.py apps/api/src/usan_api/db/session.py apps/api/tests/test_tenant_context.py
git commit -m "feat(api): per-request tenant context (set_config app.current_org)"
```

---

## Task 4: Pilot — `organization_id` + RLS on `contacts` (end-to-end proof)

**Files:**
- Create: `apps/api/migrations/versions/0031_org_id_contacts_rls.py`
- Modify: `apps/api/src/usan_api/db/models.py` (add `TenantScoped` to `Contact`)
- Test: `apps/api/tests/test_rls_isolation.py` (create — the safety net)

- [ ] **Step 1: Write the migration**

```python
"""organization_id + RLS on contacts (pilot)

Revision ID: 0031
Revises: 0030
Create Date: 2026-06-16
"""
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_ORG = "(SELECT id FROM organizations WHERE slug = 'usan')"


def upgrade() -> None:
    op.add_column("contacts", sa.Column("organization_id", sa.Uuid(), nullable=True))
    op.execute(f"UPDATE contacts SET organization_id = {_DEFAULT_ORG} WHERE organization_id IS NULL")
    op.alter_column("contacts", "organization_id", nullable=False)
    # Future inserts get the org from the tenant context (COALESCE fallback = default
    # org, so superuser seeds with no context still succeed). Set AFTER NOT NULL so the
    # ADD COLUMN above stays a fast metadata-only change (no per-row default eval).
    op.execute(
        "ALTER TABLE contacts ALTER COLUMN organization_id SET DEFAULT "
        f"COALESCE(current_setting('app.current_org', true)::uuid, {_DEFAULT_ORG})"
    )
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
```

- [ ] **Step 2: Add `TenantScoped` to the `Contact` model**

In `db/models.py`, change `class Contact(Base):` → `class Contact(Base, TenantScoped):`.

- [ ] **Step 3: Write the isolation test suite (the proof)**

Create `apps/api/tests/test_rls_isolation.py`:
```python
import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def _app_url(async_database_url: str) -> str:
    # The app role (RLS-subject). Seeds use the superuser url; queries-under-test use this.
    return async_database_url.replace("usan:usan@", "usan_app:usan_app@", 1)


async def _seed_two_orgs(super_url: str) -> tuple[str, str, str, str]:
    """As superuser (bypasses RLS): orgs A and B + one contact each. Returns ids."""
    engine = create_async_engine(super_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            a = str((await conn.execute(text(
                "INSERT INTO organizations (name, slug) VALUES ('A', :s) RETURNING id"
            ), {"s": f"a-{uuid.uuid4().hex[:8]}"})).scalar_one())
            b = str((await conn.execute(text(
                "INSERT INTO organizations (name, slug) VALUES ('B', :s) RETURNING id"
            ), {"s": f"b-{uuid.uuid4().hex[:8]}"})).scalar_one())
            ca, cb = str(uuid.uuid4()), str(uuid.uuid4())
            for cid, org in ((ca, a), (cb, b)):
                await conn.execute(text(
                    "INSERT INTO contacts (id, name, phone_e164, timezone, organization_id) "
                    "VALUES (CAST(:id AS uuid), 'C', :p, 'America/New_York', CAST(:o AS uuid))"
                ), {"id": cid, "p": f"+1{uuid.uuid4().int % 9_000_000_000 + 1_000_000_000}", "o": org})
        return a, b, ca, cb
    finally:
        await engine.dispose()


def test_rls_blocks_cross_tenant_reads(async_database_url):
    async def run():
        org_a, _org_b, ca, cb = await _seed_two_orgs(async_database_url)
        app = create_async_engine(_app_url(async_database_url), poolclass=NullPool)
        try:
            async with app.connect() as conn:
                await conn.execute(text("SELECT set_config('app.current_org', :o, false)"),
                                   {"o": org_a})
                ids = {str(r) for r in (await conn.execute(text("SELECT id FROM contacts"))).scalars()}
                assert ca in ids and cb not in ids  # only A's row, no app-layer filter
                got = (await conn.execute(
                    text("SELECT id FROM contacts WHERE id = CAST(:id AS uuid)"), {"id": cb}
                )).first()
                assert got is None
        finally:
            await app.dispose()
    asyncio.run(run())


def test_rls_fails_closed_without_context(async_database_url):
    async def run():
        await _seed_two_orgs(async_database_url)
        app = create_async_engine(_app_url(async_database_url), poolclass=NullPool)
        try:
            async with app.connect() as conn:
                rows = (await conn.execute(text("SELECT id FROM contacts"))).scalars().all()
                assert rows == []  # no context => zero rows
        finally:
            await app.dispose()
    asyncio.run(run())


def test_rls_with_check_blocks_wrong_org_insert(async_database_url):
    async def run():
        org_a, org_b, _, _ = await _seed_two_orgs(async_database_url)
        app = create_async_engine(_app_url(async_database_url), poolclass=NullPool)
        try:
            async with app.connect() as conn:
                await conn.execute(text("SELECT set_config('app.current_org', :o, false)"),
                                   {"o": org_a})
                with pytest.raises(Exception):
                    await conn.execute(text(
                        "INSERT INTO contacts (id, name, phone_e164, timezone, organization_id) "
                        "VALUES (gen_random_uuid(), 'X', '+15550000000', 'America/New_York', "
                        "CAST(:o AS uuid))"
                    ), {"o": org_b})
        finally:
            await app.dispose()
    asyncio.run(run())
```
> These tests use `set_config(..., false)` (session-scoped) on a dedicated NullPool engine because each test owns its connection. The *app* uses `is_local=true` (transaction-scoped) via `get_db`.

- [ ] **Step 4: Run the migration + isolation tests**

Run: `cd apps/api && uv run pytest tests/test_rls_isolation.py -v`
Expected: PASS (cross-tenant read blocked even with no app filter; fail-closed with no context; WITH CHECK blocks wrong-org insert). **If `test_rls_blocks_cross_tenant_reads` sees both rows, RLS is being bypassed — confirm the test connects as `usan_app` and that 0029 set `NOBYPASSRLS`.**

- [ ] **Step 5: Run the full suite**

Run: `cd apps/api && uv run pytest -q`
Expected: PASS. **Harness note (important):** the `client`/`sso_client` fixtures override `get_db` with `_override_get_db`, which yields a session from the **superuser `usan`** engine and does NOT set tenant context — so endpoint tests *bypass* RLS (superusers ignore it) and pass unchanged. That is expected; RLS is proven by `test_rls_isolation.py` (which connects as `usan_app`), not by the endpoint suite. The paths that DO run as `usan_app` in tests are the process-global-engine paths (BackgroundTasks like `flush_pending_sms`, and the workers); if any such path lacks context it fails closed here — that's a real prod bug, fixed in Task 6. Prod request handlers are covered because the real `get_db` (Task 3) sets context for every `Depends(get_db)` handler.

- [ ] **Step 6: Commit**

```bash
git add apps/api/migrations/versions/0031_org_id_contacts_rls.py apps/api/src/usan_api/db/models.py apps/api/tests/test_rls_isolation.py
git commit -m "feat(api): organization_id + fail-closed RLS on contacts (pilot) + isolation suite"
```

---

## Task 5: Fan out `organization_id` + RLS to the remaining 27 tables

**Files:**
- Create: `apps/api/migrations/versions/0032_org_id_rls_fanout.py`
- Modify: `apps/api/src/usan_api/db/models.py` (add `TenantScoped` to the other 27 models)
- Test: extend `apps/api/tests/test_rls_isolation.py`

- [ ] **Step 1: Write the DRY fan-out migration**

```python
"""organization_id + RLS on the remaining 27 tenant tables

Revision ID: 0032
Revises: 0031
Create Date: 2026-06-16
"""
from collections.abc import Sequence
from alembic import op
import sqlalchemy as sa

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# contacts handled in 0031. admin_users + organizations stay global.
_TABLES = [
    "dnc_list", "calls", "transcripts", "wellness_logs", "medication_logs",
    "medication_reminders", "personal_facts", "conversation_summaries",
    "wellbeing_survey_results", "activity_history", "turn_metrics", "call_metrics",
    "agent_profiles", "agent_profile_versions", "call_schedules", "call_batches",
    "call_batch_targets", "webhook_endpoints", "webhook_deliveries", "custom_variables",
    "admin_audit_log", "follow_up_flags", "callback_requests", "sms_messages",
    "family_contacts", "family_tasks", "family_reports",
]
_DEFAULT_ORG = "(SELECT id FROM organizations WHERE slug = 'usan')"


def upgrade() -> None:
    for t in _TABLES:
        op.add_column(t, sa.Column("organization_id", sa.Uuid(), nullable=True))
        op.execute(f"UPDATE {t} SET organization_id = {_DEFAULT_ORG} WHERE organization_id IS NULL")
        op.alter_column(t, "organization_id", nullable=False)
        op.execute(
            f"ALTER TABLE {t} ALTER COLUMN organization_id SET DEFAULT "
            f"COALESCE(current_setting('app.current_org', true)::uuid, {_DEFAULT_ORG})"
        )
        op.create_foreign_key(f"fk_{t}_organization", t, "organizations", ["organization_id"], ["id"])
        op.create_index(f"ix_{t}_organization_id", t, ["organization_id"])
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {t} "
            f"USING (organization_id = current_setting('app.current_org', true)::uuid) "
            f"WITH CHECK (organization_id = current_setting('app.current_org', true)::uuid)"
        )


def downgrade() -> None:
    for t in reversed(_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {t}")
        op.execute(f"ALTER TABLE {t} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
        op.drop_index(f"ix_{t}_organization_id", table_name=t)
        op.drop_constraint(f"fk_{t}_organization", t, type_="foreignkey")
        op.drop_column(t, "organization_id")
```
> Prod note: each `UPDATE` is a full rewrite. In tests these tables are tiny. For prod Cloud SQL, `calls`/`transcripts`/`turn_metrics`/`call_metrics` may be large — run in a maintenance window or batch the UPDATEs (P7). Document the chosen approach in the deploy PR.

- [ ] **Step 2: Add `TenantScoped` to the remaining 27 model classes**

In `db/models.py`, change each `class X(Base):` → `class X(Base, TenantScoped):` for:
`DNCEntry, Call, Transcript, WellnessLog, MedicationLog, MedicationReminder, PersonalFact, ConversationSummary, WellbeingSurveyResult, ActivityHistory, TurnMetrics, CallMetrics, AgentProfile, AgentProfileVersion, CallSchedule, CallBatch, CallBatchTarget, WebhookEndpoint, WebhookDelivery, CustomVariable, AdminAuditLog, FollowUpFlag, CallbackRequest, SmsMessage, FamilyContact, FamilyTask, FamilyReport`.

- [ ] **Step 3: Add the table-wide RLS assertion test**

Append to `apps/api/tests/test_rls_isolation.py`:
```python
@pytest.mark.parametrize("table", ["calls", "agent_profiles", "admin_audit_log", "sms_messages"])
def test_every_tenant_table_is_rls_enabled_and_fails_closed(async_database_url, table):
    async def run():
        eng = create_async_engine(_app_url(async_database_url), poolclass=NullPool)
        try:
            async with eng.connect() as conn:
                rows = (await conn.execute(text(f"SELECT 1 FROM {table}"))).all()
                assert rows == []  # no context => fail closed
                meta = (await conn.execute(text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"
                ), {"t": table})).one()
                assert meta[0] is True and meta[1] is True
        finally:
            await eng.dispose()
    asyncio.run(run())
```

- [ ] **Step 4: Run isolation + full suite**

Run: `cd apps/api && uv run pytest tests/test_rls_isolation.py -v` → PASS.
Run: `cd apps/api && uv run pytest -q` → PASS. **Same harness note as Task 4 Step 5:** endpoint tests bypass RLS (superuser `usan` via `_override_get_db`), so they pass unchanged; RLS is proven by `test_rls_isolation.py` (as `usan_app`). The high-value catch here is any **process-global-engine / BackgroundTask** path running as `usan_app` without context — it now fails closed. Triage each: it's a non-`get_db` session site that Task 6 must route through `session_in_default_org()`. Note which tests fail so Task 6 covers exactly those paths.

- [ ] **Step 5: Commit**

```bash
git add apps/api/migrations/versions/0032_org_id_rls_fanout.py apps/api/src/usan_api/db/models.py apps/api/tests/test_rls_isolation.py
git commit -m "feat(api): organization_id + RLS on all remaining tenant tables"
```

---

## Task 6: Background workers set tenant context

**Why:** the schedule orchestrator, retry/dial poller, webhook-delivery worker, and family-report job open their own sessions outside `get_db`. Under RLS they fail closed unless they set context. P1: set the default org via `session_in_default_org()` (added in Task 3). P2 makes this per-org (Open Question 1).

**Files:**
- Modify: the worker entrypoints that create sessions directly.
- Test: `apps/api/tests/test_worker_tenant_context.py` (create)

- [ ] **Step 1: Identify the worker session sites**

Run: `cd apps/api && grep -rn "get_session_factory\|async_sessionmaker\|get_engine(" src/usan_api | grep -viE "db/session.py|tenant_context.py"`
Expected hits: the schedule orchestrator loop, the retry/dial poller, the webhook delivery worker, `family_report_job`, and **any BackgroundTask that uses the process-global engine (e.g. `flush_pending_sms`)**. **Completeness is the security property here:** these non-`get_db` session sites are the ONLY prod paths that bypass the `get_db` context chokepoint, so every one must set context. Cross-check this grep against the exact set of tests that failed-closed in Task 4 Step 5 / Task 5 Step 4 — every failing path must appear here and get fixed. Each opens a session per unit of work — that's where context must be set.

- [ ] **Step 2: Write a worker-context test**

Create `apps/api/tests/test_worker_tenant_context.py`:
```python
import asyncio
from sqlalchemy import text
from usan_api.tenant_context import session_in_default_org


def test_worker_session_has_context(client):  # client fixture sets app DB env to usan_app
    async def run():
        async with session_in_default_org() as s:
            val = (await s.execute(
                text("SELECT current_setting('app.current_org', true)")
            )).scalar_one()
            await s.execute(text("SELECT 1 FROM contacts"))  # not fail-closed
            return val
    assert asyncio.run(run())
```
Run: `cd apps/api && uv run pytest tests/test_worker_tenant_context.py -v` → PASS (helper exists from Task 3).

- [ ] **Step 3: Route each worker's session through the helper**

For each site from Step 1, replace `async with get_session_factory()() as session:` (or the equivalent `async_sessionmaker(...)()` call) with `async with session_in_default_org() as session:`. Add `from usan_api.tenant_context import session_in_default_org`. Keep all other logic identical.

- [ ] **Step 4: Run the worker + orchestration tests**

Run: `cd apps/api && uv run pytest tests/test_worker_tenant_context.py tests/test_schedule_orchestrator.py tests/test_retry_orchestrator.py -q` (and any webhook/family-report test files present).
Expected: PASS. A failure indicates a worker path still opening a context-free session — route it through `session_in_default_org()`.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/tenant_context.py apps/api/tests/test_worker_tenant_context.py
git add -u  # the changed worker files
git commit -m "feat(api): background workers run under default-org tenant context"
```

---

## Task 7: Green gate + prod role cutover docs

**Files:**
- Modify: `infra/terraform/database.tf`, `infra/.env.prod.example`
- No app code.

- [ ] **Step 1: Full green gate**

Run:
```bash
cd apps/api && ruff check . && ruff format --check . && uv run mypy && uv run pytest -q
```
Expected: all green. The full suite running as `usan_app` under RLS with default-org context IS the behavior-preservation proof.

- [ ] **Step 2: Add the prod `usan_app` Cloud SQL user (Terraform)**

In `infra/terraform/database.tf`, mirror the existing `grafana_ro` user block:
```hcl
resource "random_password" "usan_app" {
  length  = 32
  special = false
}

resource "google_sql_user" "usan_app" {
  name     = "usan_app"
  instance = google_sql_database_instance.usan.name
  password = random_password.usan_app.result
}
```
The role + grants already exist from migration 0029; Terraform only provisions the login + password (same split as `grafana_ro`).

- [ ] **Step 3: Document the prod DATABASE_URL cutover**

In `infra/.env.prod.example`, add a commented note: the API's `DATABASE_URL` must point at `usan_app` (NOT `usan`) for RLS to protect production — RLS is bypassed by `usan` (cloudsqlsuperuser). Include the one-time prod verification:
```
-- Confirm the app role cannot bypass RLS (both must be false):
-- SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'usan_app';
```
> This cutover (point `DATABASE_URL` at `usan_app`) is what activates protection in prod and is performed during the deploy of this change — call it out explicitly in the deploy PR. Until then, RLS is enforced in CI but bypassed by the prod `usan` connection.

- [ ] **Step 4: Migration round-trip check**

Run: `cd apps/api && uv run alembic upgrade head && uv run alembic downgrade 0028 && uv run alembic upgrade head`
Expected: clean up and down (no errors), confirming the migrations reverse cleanly.

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/database.tf infra/.env.prod.example
git commit -m "chore(infra): provision usan_app Cloud SQL login + document RLS cutover"
```

---

## Final verification before PR

- [ ] `cd apps/api && ruff check . && uv run mypy && uv run pytest -q` → green (full suite under `usan_app` + RLS).
- [ ] `uv run pytest tests/test_rls_isolation.py -v` → cross-tenant blocked, fail-closed, WITH CHECK enforced, all sampled tenant tables RLS-enabled+forced.
- [ ] Migrations round-trip `0028 ↔ head` cleanly (Task 7 Step 4).
- [ ] Open PR `feat/tenancy-foundation` → `main`. PR body MUST state: (a) this is P1 only / behavior-preserving; (b) prod activation = point `DATABASE_URL` at `usan_app` + run the `rolbypassrls` verification; (c) large-table backfill ran in a maintenance window (or was batched).
