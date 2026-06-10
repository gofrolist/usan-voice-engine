import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer

from usan_api.db.session import get_db
from usan_api.main import create_app
from usan_api.settings import get_settings

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


@pytest.fixture(scope="session")
def database_url() -> str:
    with PostgresContainer(
        "pgvector/pgvector:pg18", username="usan", password="usan", dbname="usan"
    ) as pg:
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
        yield url


@pytest.fixture(scope="session")
def async_database_url(database_url: str) -> str:
    return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)


async def _truncate_and_dispose(engine: AsyncEngine) -> None:
    # Reset table state then dispose, run from the client teardown — so pure-unit
    # tests that never request `client` don't pay for a Postgres container.
    from usan_api.db.session import dispose_engine

    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE call_batch_targets, call_batches, call_schedules, "
                    "agent_profile_versions, agent_profiles, admin_audit_log, "
                    "admin_users, follow_up_flags, callback_requests, sms_messages, "
                    "calls, dnc_list, elders "
                    "RESTART IDENTITY CASCADE"
                )
            )
    finally:
        await engine.dispose()
        # Also dispose the process-global engine (used by BackgroundTasks like
        # flush_pending_sms). It's lazily bound to the loop of whichever request
        # first opened it; without resetting it, the next `client` fixture runs on
        # a fresh loop and reuses a now-dead engine -> "Event loop is closed".
        await dispose_engine()


@pytest.fixture
def client(database_url: str, async_database_url: str, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", database_url)
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
    get_settings.cache_clear()

    test_engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def _override_get_db():
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        asyncio.run(_truncate_and_dispose(test_engine))
        get_settings.cache_clear()


@pytest.fixture
def operator_headers() -> dict[str, str]:
    return {"Authorization": "Bearer " + "o" * 32}


@pytest.fixture
def sso_client(database_url: str, async_database_url: str, monkeypatch) -> TestClient:
    """Like `client`, but with Google SSO configured (for /v1/auth flow tests)."""
    monkeypatch.setenv("DATABASE_URL", database_url)
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
    factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def _override_get_db():
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app = create_app()
    app.dependency_overrides[get_db] = _override_get_db
    try:
        # follow_redirects off so the login 302 to Google is observable.
        yield TestClient(app, follow_redirects=False)
    finally:
        asyncio.run(_truncate_and_dispose(test_engine))
        get_settings.cache_clear()


async def _seed_admin_user_async(async_database_url: str, email: str, role: str) -> None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, role, added_by) "
                    "VALUES (:email, CAST(:role AS admin_role), 'test') "
                    "ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role"
                ),
                {"email": email.lower(), "role": role},
            )
    finally:
        await engine.dispose()


@pytest.fixture
def admin_session(client: TestClient, async_database_url: str) -> dict[str, str]:
    """Seed an allow-listed admin and return cookies that authenticate as them.

    Sets the cookie on the shared `client` too, so tests can either rely on the
    client jar or pass `cookies=admin_session` per request.
    """
    from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
    from usan_api.db.base import AdminRole

    email = "admin@example.com"
    asyncio.run(_seed_admin_user_async(async_database_url, email, "admin"))
    token = issue_session(email, AdminRole.ADMIN, get_settings())
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return {SESSION_COOKIE_NAME: token}
