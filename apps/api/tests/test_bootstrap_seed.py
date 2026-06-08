import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.session import dispose_engine, get_db
from usan_api.main import create_app
from usan_api.settings import get_settings

TEST_SECRET = "a" * 32


async def _emails(async_database_url: str) -> set[str]:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(text("SELECT email FROM admin_users"))
            return {r[0] for r in rows}
    finally:
        await engine.dispose()


def test_lifespan_seeds_bootstrap_emails(database_url, async_database_url, monkeypatch):
    async def _truncate():
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(text("TRUNCATE admin_users RESTART IDENTITY CASCADE"))
        finally:
            await engine.dispose()

    asyncio.run(_truncate())

    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", TEST_SECRET)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAILS", "Founder@Example.com, ops@example.com")
    monkeypatch.setenv("RETRY_POLLER_ENABLED", "false")
    get_settings.cache_clear()

    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_get_db():
        async with factory() as session:
            yield session

    # The lifespan's bootstrap seed uses the global get_session_factory(); reset the
    # cached engine so it rebuilds against THIS test's DATABASE_URL (a prior test may
    # have initialized the global with a dummy URL).
    asyncio.run(dispose_engine())

    app = create_app()
    app.dependency_overrides[get_db] = _override_get_db
    try:
        with TestClient(app):  # entering the context runs lifespan startup
            pass
        emails = asyncio.run(_emails(async_database_url))
        assert {"founder@example.com", "ops@example.com"} <= emails
    finally:
        asyncio.run(engine.dispose())
        get_settings.cache_clear()
