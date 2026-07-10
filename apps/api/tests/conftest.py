import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import jwt
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer

from usan_api.auth import get_tenant_db
from usan_api.db.session import get_db
from usan_api.main import create_app
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context

API_DIR = Path(__file__).resolve().parents[1]
TEST_SECRET = "a" * 32

# Shared auth helpers (the operator bearer header + a service JWT minted with the
# same JWT_SIGNING_KEY the `client` fixture sets). Several test modules need these;
# keep one copy here so a token-format change is a single edit.
OPERATOR_HEADERS = {"Authorization": "Bearer " + "o" * 32}


def service_token(call_id: str, secret: str = "s" * 32) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )


def counter_value(counter, **labels) -> float:
    """Read a Counter's cumulative value via the public collect() API.

    Avoids the private `._value.get()` internal. The `_total` sample carries the
    cumulative count; `labels` filters labeled counters (empty for unlabeled ones).
    """
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total") and sample.labels == labels:
                return sample.value
    return 0.0


def gauge_value(gauge) -> float:
    """Read an unlabeled Gauge's current value via the public collect() API.

    Same collect()-based discipline as counter_value: an unlabeled Gauge exposes
    exactly one sample whose name equals the family name.
    """
    for metric in gauge.collect():
        for sample in metric.samples:
            if sample.name == metric.name and sample.labels == {}:
                return sample.value
    return 0.0


async def _set_app_role_password(super_url: str) -> None:
    """Give the migration-created usan_app role a known login password.

    usan_app is created by migration 0029 with LOGIN but no password (secrets never
    live in migrations). The app-under-test (and the RLS isolation suite) connect as
    this RLS-subject role, so the test harness sets a known password here. Runs as the
    superuser url (asyncpg fallback — psycopg v3 is not installed in this env).
    """
    eng = create_async_engine(
        super_url.replace("postgresql://", "postgresql+asyncpg://", 1),
        poolclass=NullPool,
    )
    try:
        async with eng.begin() as conn:
            await conn.execute(text("ALTER ROLE usan_app WITH LOGIN PASSWORD 'usan_app'"))
    finally:
        await eng.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    # Durability off for the throwaway test DB: the suite is dominated by per-test
    # TRUNCATE + insert I/O, and with fsync on every commit waits on a WAL flush.
    # fsync/synchronous_commit/full_page_writes are safe to disable here — if the
    # container crashes mid-run the whole test run is void anyway. tmpfs replaces
    # the image's anonymous volume so checkpoints/bgwriter never touch real disk
    # either (pg18's PGDATA is /var/lib/postgresql/18/docker, under this mount).
    container = (
        PostgresContainer("pgvector/pgvector:pg18", username="usan", password="usan", dbname="usan")
        .with_kwargs(tmpfs={"/var/lib/postgresql": "rw"})
        .with_command("postgres -c fsync=off -c synchronous_commit=off -c full_page_writes=off")
    )
    with container as pg:
        host = pg.get_container_host_ip()
        port = pg.get_exposed_port(5432)
        url = f"postgresql://usan:usan@{host}:{port}/usan"
        env = {
            **os.environ,
            "DATABASE_URL": url,
            "LIVEKIT_API_KEY": "key",
            "LIVEKIT_API_SECRET": TEST_SECRET,
            "LIVEKIT_URL": "ws://livekit:7880",
            "JWT_SIGNING_KEY": "s" * 32,
            "OPERATOR_API_KEY": "o" * 32,
        }
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=API_DIR,
            env=env,
            check=True,
        )
        # usan_app is created by migration 0029 with LOGIN but no password (secrets
        # never live in migrations). Give it a known test password so the
        # app-under-test (and the isolation suite) connect as the RLS-subject role.
        asyncio.run(_set_app_role_password(url))
        yield url


@pytest.fixture(scope="session")
def async_database_url(database_url: str) -> str:
    return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)


# NOTE: usan_app's password is set once by `database_url` above. The only test that
# can break it is test_migration_roundtrip.py (the sole below-0029 downgrade, which
# recreates usan_app passwordless) — and it re-applies the ALTER ROLE in its own
# `finally`, so no per-test healing fixture is needed.


@pytest.fixture(scope="session")
def app_database_url(database_url: str) -> str:
    """The usan_app (non-superuser, RLS-subject) DSN, derived from the superuser url."""
    return database_url.replace("usan:usan@", "usan_app:usan_app@", 1)


@pytest.fixture(scope="session")
def app_async_database_url(app_database_url: str) -> str:
    """The async (asyncpg) usan_app DSN — the RLS-subject role for repo/route tests."""
    return app_database_url.replace("postgresql://", "postgresql+asyncpg://", 1)


@pytest.fixture
def two_orgs(async_database_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert org A + org B via the superuser engine; yield their ids.

    On teardown, delete the two orgs and their memberships. The membership delete
    runs first (FK), then the orgs; ON DELETE CASCADE would also cover memberships,
    but the explicit delete keeps the cleanup independent of the FK action.
    """

    async def _setup() -> tuple[uuid.UUID, uuid.UUID]:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                a = (
                    await conn.execute(
                        text(
                            "INSERT INTO organizations (name, slug) VALUES ('A', :s) RETURNING id"
                        ),
                        {"s": f"a-{uuid.uuid4().hex[:8]}"},
                    )
                ).scalar_one()
                b = (
                    await conn.execute(
                        text(
                            "INSERT INTO organizations (name, slug) VALUES ('B', :s) RETURNING id"
                        ),
                        {"s": f"b-{uuid.uuid4().hex[:8]}"},
                    )
                ).scalar_one()
            return a, b
        finally:
            await engine.dispose()

    async def _teardown(org_a: uuid.UUID, org_b: uuid.UUID) -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                # Safety net: never hang if a test left an open transaction holding an
                # FK lock on these orgs (membership insert takes FOR KEY SHARE) — fail
                # fast instead. The app_session fixture rolls back before this runs, so
                # under correct fixture ordering the lock is already released.
                await conn.execute(text("SET LOCAL lock_timeout = '5s'"))
                await conn.execute(
                    text("DELETE FROM memberships WHERE organization_id IN (:a, :b)"),
                    {"a": org_a, "b": org_b},
                )
                await conn.execute(
                    text("DELETE FROM organizations WHERE id IN (:a, :b)"),
                    {"a": org_a, "b": org_b},
                )
        finally:
            await engine.dispose()

    org_a, org_b = asyncio.run(_setup())
    try:
        yield org_a, org_b
    finally:
        asyncio.run(_teardown(org_a, org_b))


@pytest_asyncio.fixture
async def app_session(app_async_database_url: str):
    """A usan_app async session (NullPool) for repo-level tests.

    Connects as the non-superuser RLS-subject role. memberships + admin_users are
    GLOBAL (non-RLS) tables, so repo tests need no org context; tests that exercise
    RLS-scoped tables must call set_tenant_context(session, org_id) themselves.
    Rolls back on teardown so each test is isolated even without a TRUNCATE.
    """
    engine = create_async_engine(app_async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        await session.rollback()
        await session.close()
        await engine.dispose()


# Every table a `client` test may touch, wiped as one statement. RESTART IDENTITY
# CASCADE so dependent rows (e.g. webhook_deliveries -> webhook_endpoints) go too.
_TRUNCATE_ALL = (
    "TRUNCATE transcripts, wellness_logs, medication_logs, medication_reminders, "
    "personal_facts, conversation_summaries, wellbeing_survey_results, "
    "activity_history, turn_metrics, call_metrics, "
    "family_contacts, family_tasks, family_reports, "
    "custom_variables, webhook_deliveries, webhook_endpoints, "
    "compat_webhook_deliveries, compat_webhook_endpoints, phone_numbers, "
    "call_batch_targets, call_batches, call_schedules, "
    "agent_profile_versions, agent_profiles, admin_audit_log, compat_api_keys, "
    "invitations, memberships, admin_users, follow_up_flags, callback_requests, sms_messages, "
    "knowledge_base_chunks, knowledge_base_sources, knowledge_bases, "
    "calls, dnc_list, contacts "
    "RESTART IDENTITY CASCADE"
)


async def _truncate(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(_TRUNCATE_ALL))


async def _truncate_and_dispose(engine: AsyncEngine) -> None:
    # Reset table state then dispose, run from the client teardown — so pure-unit
    # tests that never request `client` don't pay for a Postgres container.
    from usan_api.db.session import dispose_engine

    try:
        await _truncate(engine)
    finally:
        await engine.dispose()
        # Also dispose the process-global engine (used by BackgroundTasks like
        # flush_pending_sms). It's lazily bound to the loop of whichever request
        # first opened it; without resetting it, the next `client` fixture runs on
        # a fresh loop and reuses a now-dead engine -> "Event loop is closed".
        await dispose_engine()


async def _truncate_and_resolve_org(engine: AsyncEngine) -> uuid.UUID:
    """TRUNCATE the client-test tables AND read the seeded usan org id on ONE connection.

    The `client`/`sso_client` setup needs both the clean-before truncate and the default
    org id. Doing them on a single NullPool connection (instead of `_truncate` + a separate
    `_resolve_usan_org_id` engine) saves one asyncpg connect + event-loop spin-up per client
    test. The org id is still re-read every test — never cached — because the migration
    round-trip suite downgrades below 0030 and re-upgrades, reseeding `organizations` with a
    fresh gen_random_uuid(); `organizations` is intentionally NOT in `_TRUNCATE_ALL`, so the
    seeded usan row survives the TRUNCATE and the SELECT in the same transaction sees it.
    """
    async with engine.begin() as conn:
        await conn.execute(text(_TRUNCATE_ALL))
        org_id = (
            await conn.execute(text("SELECT id FROM organizations WHERE slug = 'usan'"))
        ).scalar_one_or_none()
    if org_id is None:
        raise RuntimeError(
            "default organization (slug='usan') is not seeded; "
            "the connect-baseline org seed must run before the test override"
        )
    return org_id


# create_app() rebuilds the entire FastAPI router tree (~40-120ms; cProfile put it
# at the top of client-test cost). The routed app is identical across every
# `client`/`sso_client` test: rate limiting and docs are off at build time for all
# of them, while SSO config and the DB engine are read PER REQUEST, never baked in.
# So build it once per worker and only swap dependency_overrides[get_db] per test.
# Prometheus collectors are registered exactly once regardless (instrumentation.py).
_ROUTED_APP: FastAPI | None = None


def _routed_app() -> FastAPI:
    global _ROUTED_APP
    if _ROUTED_APP is None:
        _ROUTED_APP = create_app()
    return _ROUTED_APP


async def _resolve_usan_org_id(super_url: str) -> uuid.UUID:
    """The seeded default (usan) org id, read via the superuser engine.

    Used as the default active org for the `get_tenant_db` test override so admin
    routes scope to the same org the `admin_session` fixture mints into.
    """
    engine = create_async_engine(super_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            org_id = (
                await conn.execute(text("SELECT id FROM organizations WHERE slug = 'usan'"))
            ).scalar_one_or_none()
            if org_id is None:
                raise RuntimeError(
                    "default organization (slug='usan') is not seeded; "
                    "the connect-baseline org seed must run before the test override"
                )
            return org_id
    finally:
        await engine.dispose()


def act_as_org(app: FastAPI, org_id: uuid.UUID) -> None:
    """Repoint the `get_tenant_db` test override at ``org_id``.

    Admin routes resolve their org from the session principal in production; under
    test the `get_tenant_db` override scopes the usan_app session to a single
    test-controlled active org (default: the seeded usan org). Multi-org / act-as
    tests call this to read/write as a different active org without minting a new
    cookie. State lives on ``app.state`` so the override closure reads it per request.
    """
    app.state.test_active_org_id = org_id


def _install_tenant_db_override(
    app: FastAPI, app_async_database_url: str, default_org_id: uuid.UUID
) -> AsyncEngine:
    """Override `get_tenant_db` with a per-test usan_app session under RLS context.

    The production dependency opens a session on the process-global engine — bound to
    a different event loop than the per-test TestClient, which crashes with
    "attached to a different loop". This override mirrors the `get_db` override: a
    per-test asyncpg engine (NullPool) connecting as the non-superuser usan_app role
    so RLS applies, with the tenant context set to the test-controlled active org
    (mutable via `act_as_org`, default the seeded usan org).
    """
    from usan_api.db.session import _install_default_org_context

    app.state.test_active_org_id = default_org_id
    tenant_engine = create_async_engine(app_async_database_url, poolclass=NullPool)
    # Mirror production: get_tenant_db runs on the process-global engine, which installs
    # the default-org connect baseline (set_config is_local=false — session-level, so it
    # survives COMMIT). Handlers commit then db.refresh(); without this baseline the
    # post-commit refresh SELECT would run context-free and RLS would hide the just-
    # written row ("Could not refresh instance"). set_tenant_context (is_local=true) still
    # overrides per-transaction; at txn end it reverts to this connect default.
    _install_default_org_context(tenant_engine)
    factory = async_sessionmaker(tenant_engine, expire_on_commit=False)

    async def _override_get_tenant_db():
        org_id = str(app.state.test_active_org_id)

        def _reapply_org_context(_session, _transaction, connection) -> None:
            # Mirror production get_tenant_db: set_tenant_context is is_local=true, so it
            # reverts to the connect baseline at COMMIT. A handler that reads after commit
            # (create_profile's db.refresh) would then run under the default-org baseline
            # and RLS would hide a row written into a non-default active org (act-as).
            # Re-apply per transaction so post-commit reads stay scoped to the active org.
            connection.execute(
                text("SELECT set_config('app.current_org', :org, true)"), {"org": org_id}
            )

        async with factory() as session:
            event.listen(session.sync_session, "after_begin", _reapply_org_context)
            try:
                await set_tenant_context(session, app.state.test_active_org_id)
                yield session
            except Exception:
                await session.rollback()
                raise
            finally:
                event.remove(session.sync_session, "after_begin", _reapply_org_context)

    app.dependency_overrides[get_tenant_db] = _override_get_tenant_db
    return tenant_engine


@pytest.fixture
def client(
    database_url: str,
    async_database_url: str,
    app_database_url: str,
    app_async_database_url: str,
    monkeypatch,
) -> TestClient:
    # Connect the app-under-test as the non-superuser usan_app role so RLS applies.
    monkeypatch.setenv("DATABASE_URL", app_database_url)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", TEST_SECRET)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", "ST_test")
    monkeypatch.setenv("TELNYX_CALLER_ID", "+15551230000")
    monkeypatch.setenv("AGENT_NAME", "usan-agent")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    # Absolute origin for invite accept-links (invites._origin); without it the builder
    # raises rather than emit a malformed "://..." URL.
    monkeypatch.setenv("ADMIN_BASE_URL", "http://testserver")
    get_settings.cache_clear()

    test_engine = create_async_engine(async_database_url, poolclass=NullPool)
    # Clean BEFORE yielding, not only after. The per-worker Postgres is shared with
    # modules that truncate on their own teardown (or never), so under xdist the test
    # that ran just before this one on the same worker can leave rows behind — e.g. an
    # enabled webhook_endpoint that silently inflates this request's outbox fan-out
    # (the room_finished double-fire regression test, CI run 27508993998). Starting
    # from a known-clean DB makes each client test order-independent.
    # One connection does both: clean-before truncate + read the default org id (saves a
    # separate _resolve_usan_org_id engine/connection per client test).
    usan_org_id = asyncio.run(_truncate_and_resolve_org(test_engine))
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def _override_get_db():
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = _routed_app()
    app.dependency_overrides[get_db] = _override_get_db
    tenant_engine = _install_tenant_db_override(app, app_async_database_url, usan_org_id)
    try:
        yield TestClient(app)
    finally:
        asyncio.run(_truncate_and_dispose(test_engine))
        asyncio.run(tenant_engine.dispose())
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_tenant_db, None)
        get_settings.cache_clear()
        from usan_api.tenant_context import _clear_default_org_cache

        _clear_default_org_cache()


@pytest.fixture
def operator_headers() -> dict[str, str]:
    return {"Authorization": "Bearer " + "o" * 32}


@pytest.fixture
def sso_client(
    database_url: str,
    async_database_url: str,
    app_database_url: str,
    app_async_database_url: str,
    monkeypatch,
) -> TestClient:
    """Like `client`, but with Google SSO configured (for /v1/auth flow tests)."""
    # Connect the app-under-test as the non-superuser usan_app role so RLS applies.
    monkeypatch.setenv("DATABASE_URL", app_database_url)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", TEST_SECRET)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", "ST_test")
    monkeypatch.setenv("TELNYX_CALLER_ID", "+15551230000")
    monkeypatch.setenv("AGENT_NAME", "usan-agent")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_OAUTH_REDIRECT_URI", "http://testserver/v1/auth/callback")
    get_settings.cache_clear()

    test_engine = create_async_engine(async_database_url, poolclass=NullPool)
    # clean-before truncate + default org id on one connection (see `client`).
    usan_org_id = asyncio.run(_truncate_and_resolve_org(test_engine))
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def _override_get_db():
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = _routed_app()
    app.dependency_overrides[get_db] = _override_get_db
    tenant_engine = _install_tenant_db_override(app, app_async_database_url, usan_org_id)
    try:
        # follow_redirects off so the login 302 to Google is observable.
        yield TestClient(app, follow_redirects=False)
    finally:
        asyncio.run(_truncate_and_dispose(test_engine))
        asyncio.run(tenant_engine.dispose())
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_tenant_db, None)
        get_settings.cache_clear()
        from usan_api.tenant_context import _clear_default_org_cache

        _clear_default_org_cache()


async def _seed_admin_user_async(
    async_database_url: str,
    email: str,
    role: str | None = None,
    *,
    is_super_admin: bool = False,
    with_membership: bool = True,
) -> uuid.UUID:
    """Seed a global identity (and, by default, a usan-org membership).

    Returns the usan (default) org id so callers can mint a session scoped to it.
    Role now lives on the membership, not on admin_users (P2 / migration 0033).

    ``role`` is required only when ``with_membership`` is True (it sets the
    membership role); passing it with ``with_membership=False`` is a mistake — the
    membership row that would carry it is never written — so we reject that combo
    rather than silently dropping the argument.
    """
    if with_membership and role is None:
        raise ValueError("role is required when with_membership=True")
    if not with_membership and role is not None:
        raise ValueError("role is ignored when with_membership=False; omit it")
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            org_id = (
                await conn.execute(text("SELECT id FROM organizations WHERE slug = 'usan'"))
            ).scalar_one_or_none()
            if org_id is None:
                raise RuntimeError(
                    "seed precondition failed: no organization with slug='usan' "
                    "(the connect-baseline org seed must run before admin fixtures)"
                )
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, is_super_admin, status, added_by) "
                    "VALUES (:email, :super, 'active', 'test') "
                    "ON CONFLICT (email) DO UPDATE SET is_super_admin = EXCLUDED.is_super_admin"
                ),
                {"email": email.lower(), "super": is_super_admin},
            )
            if with_membership:
                await conn.execute(
                    text(
                        "INSERT INTO memberships (email, organization_id, role, added_by) "
                        "VALUES (:email, :org, CAST(:role AS admin_role), 'test') "
                        "ON CONFLICT (email, organization_id) DO UPDATE SET role = EXCLUDED.role"
                    ),
                    {"email": email.lower(), "org": org_id, "role": role},
                )
        return org_id
    finally:
        await engine.dispose()


@pytest.fixture
def admin_session(client: TestClient, async_database_url: str) -> dict[str, str]:
    """Seed an allow-listed admin and return cookies that authenticate as them.

    The admin gets an ADMIN membership in the seeded usan (default) org, and the
    minted session carries that org as the active org (P2). Sets the cookie on the
    shared `client` too, so tests can rely on the jar or pass `cookies=admin_session`.
    """
    from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
    from usan_api.db.base import AdminRole

    email = "admin@example.com"
    org_id = asyncio.run(_seed_admin_user_async(async_database_url, email, "admin"))
    token = issue_session(
        email,
        active_org_id=org_id,
        role=AdminRole.ADMIN,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return {SESSION_COOKIE_NAME: token}


@pytest.fixture
def super_admin_session(client: TestClient, async_database_url: str) -> dict[str, str]:
    """Seed a super-admin identity with NO membership and return its cookies.

    The minted session has `super=True`, `active_org=None`, `acting_as=False` — the
    state of a USAN staffer who has not yet picked (or acted-as) an org.
    """
    from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session

    email = "super@example.com"
    asyncio.run(
        _seed_admin_user_async(
            async_database_url,
            email,
            is_super_admin=True,
            with_membership=False,
        )
    )
    token = issue_session(
        email,
        active_org_id=None,
        role=None,
        is_super_admin=True,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return {SESSION_COOKIE_NAME: token}


@pytest.fixture
def super_admin_acting_session(client: TestClient, async_database_url: str) -> dict[str, str]:
    """Seed a super-admin and return cookies for a session acting-as the usan org.

    P4 makes the profile-authoring routers (profiles, defaults, custom-variables, the
    catalogs, profile-tests) super-admin only. Tests that used to exercise those
    endpoints as a plain ADMIN swap `admin_session` -> this fixture: a USAN operator
    acting-as the seeded usan (default) org. The cookie carries `is_super_admin=True`
    (passes the router-level `require_super_admin` gate) and `role=ADMIN`
    (`acting_as=True`, so the per-route `require_admin_role(ADMIN)` write gates still
    pass). The active org matches the conftest `get_tenant_db` override's default, so
    reads/writes land in the same org `admin_session` used.
    """
    email = "staff@usan.example.com"
    org_id = asyncio.run(
        _seed_admin_user_async(
            async_database_url,
            email,
            is_super_admin=True,
            with_membership=False,
        )
    )
    from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
    from usan_api.db.base import AdminRole

    token = issue_session(
        email,
        active_org_id=org_id,
        role=AdminRole.ADMIN,
        is_super_admin=True,
        acting_as=True,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return {SESSION_COOKIE_NAME: token}


# --- Compat (RetellAI-parity, feature 003) test support -------------------------------


async def _seed_compat_key_async(
    super_async_url: str, org_id: uuid.UUID, *, status: str = "active"
) -> str:
    """Insert a compat_api_keys row for ``org_id`` (superuser engine) and return the
    one-time plaintext token. ``status`` lets a test seed a 'revoked' key for the 401 path."""
    import secrets

    from usan_api.repositories.compat_api_keys import hash_token

    token = "key_" + secrets.token_urlsafe(32)
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO compat_api_keys "
                    "(organization_id, key_prefix, key_hash, status) "
                    "VALUES (:org, :pfx, :hash, :st)"
                ),
                {"org": org_id, "pfx": token[:8], "hash": hash_token(token), "st": status},
            )
        return token
    finally:
        await engine.dispose()


def _install_compat_db_override(app: FastAPI, db_async_url: str) -> FastAPI:
    """Override ``get_compat_db`` with a per-test session that runs the REAL auth path
    (prefix lookup + HMAC verify + set_tenant_context), so the 401 + org-scoping logic stays
    real, then yields the scoped session.

    Uses the SUPERUSER DSN (like the ``get_db`` operator-plane override) rather than the
    usan_app one: under the TestClient's fresh-loop-per-request, the RLS-role engine + the
    compat write path leaves a connection bound to a dead loop (a test-harness artifact — prod
    runs one persistent loop on a pooled engine). Cross-org RLS isolation is exercised
    separately by test_compat_rls_isolation (a real usan_app session). The compat layer still
    set_tenant_context's the org, so inserts get the right organization_id via the column
    default."""
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

    from usan_api.compat import auth as compat_auth

    bearer = HTTPBearer(auto_error=False)

    async def _override(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ):
        # A FRESH engine per request, created + disposed within THIS request's event loop, so
        # no connection is ever touched from another loop. The TestClient uses a new event
        # loop per request, and the heavy compat write path otherwise strands a connection on
        # the prior loop -> "Event loop is closed" on the next request. The
        # set_tenant_context / after_begin plumbing is covered by test_compat_auth +
        # test_compat_rls_isolation; here the superuser engine + the organization_id column
        # default land inserts in the right org.
        token = compat_auth._require_bearer(credentials)
        engine = create_async_engine(db_async_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                matched = await compat_auth._match_key(session, token)
                request.state.compat_org_id = str(matched.organization_id)
                yield session
        finally:
            await engine.dispose()

    # The compat routes live on the MOUNTED sub-app, which has its OWN dependency_overrides;
    # overriding on the outer app would NOT reach them — they'd use the production
    # get_compat_db + the global pooled engine, whose connection is bound to the first
    # request's event loop and dies on the next ("Event loop is closed").
    from starlette.routing import Mount

    compat_app = next(r.app for r in app.routes if isinstance(r, Mount))
    compat_app.dependency_overrides[compat_auth.get_compat_db] = _override
    return compat_app


@pytest.fixture
def compat_client(client: TestClient, async_database_url: str) -> TestClient:
    """``client`` + the ``get_compat_db`` override installed, so requests to the mounted
    RetellAI-compatible sub-app run under a per-test org-scoped session."""
    from usan_api.compat.auth import get_compat_db

    compat_app = _install_compat_db_override(client.app, async_database_url)
    try:
        yield client
    finally:
        compat_app.dependency_overrides.pop(get_compat_db, None)


@pytest.fixture
def compat_headers(compat_client: TestClient, async_database_url: str) -> dict[str, str]:
    """Seed an active compat key for the seeded usan org; return its Bearer auth header."""
    org_id = asyncio.run(_resolve_usan_org_id(async_database_url))
    token = asyncio.run(_seed_compat_key_async(async_database_url, org_id))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def compat_env(database_url: str, async_database_url: str, monkeypatch):
    """Minimal env + clean tables for compat WEBHOOK poller tests (US2) that drive sessions
    directly via the superuser engine and call ``get_settings()`` / ``poll_once`` WITHOUT
    building the app. Truncates BEFORE yielding so each test starts from an empty
    compat_webhook_deliveries (the lifecycle assertions count rows unfiltered)."""
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", TEST_SECRET)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("TELNYX_CALLER_ID", "+15551230000")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    get_settings.cache_clear()
    asyncio.run(_truncate_and_dispose(create_async_engine(async_database_url, poolclass=NullPool)))
    yield
    get_settings.cache_clear()


def _create_and_publish_seed_agent(client: TestClient, headers: dict) -> str:
    """Create a compat LLM + agent named 'Seed Agent', publish version 1, return agent_id.

    Shared helper for fixtures that need a published default outbound profile.
    """
    from usan_api.compat.voice_map import to_retell_voice_id
    from usan_api.schemas.voice_catalog import VOICE_CATALOG

    retell_voice = to_retell_voice_id(VOICE_CATALOG[0].cartesia_voice_id)
    llm = client.post(
        "/create-retell-llm",
        json={"start_speaker": "agent", "general_prompt": "hi"},
        headers=headers,
    ).json()
    agent = client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "voice_id": retell_voice,
            "agent_name": "Seed Agent",
        },
        headers=headers,
    ).json()
    agent_id = agent["agent_id"]
    client.post(f"/publish-agent-version/{agent_id}", json={"version": 1}, headers=headers)
    return agent_id


@pytest.fixture
def published_default_agent(
    compat_client: TestClient, compat_headers: dict, async_database_url: str
) -> str:
    """Publish a 'Seed Agent' via the compat API and mark it the ACTIVE default OUTBOUND
    profile via a direct superuser UPDATE. Returns the compat agent_id.

    Used by tests that exercise the no-override create-phone-call path — the guard added in
    Phase 1b Task 1 requires a published default to exist when no override_agent_id is given.
    """
    agent_id = _create_and_publish_seed_agent(compat_client, compat_headers)

    async def _mark_default() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE agent_profiles SET is_default_outbound = true WHERE name = :name"),
                    {"name": "Seed Agent"},
                )
        finally:
            await engine.dispose()

    asyncio.run(_mark_default())
    return agent_id
