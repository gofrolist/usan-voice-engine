import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from testcontainers.postgres import PostgresContainer

from usan_api.db.session import get_db
from usan_api.main import create_app
from usan_api.settings import get_settings

API_DIR = Path(__file__).resolve().parents[1]
TEST_SECRET = "a" * 32


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


async def _truncate(async_url: str) -> None:
    engine = create_async_engine(async_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE calls, dnc_list, elders RESTART IDENTITY CASCADE"))
    await engine.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(async_database_url: str):
    yield
    asyncio.run(_truncate(async_database_url))


@pytest.fixture
def client(database_url: str, async_database_url: str, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", TEST_SECRET)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("LIVEKIT_SIP_OUTBOUND_TRUNK_ID", "ST_test")
    monkeypatch.setenv("TELNYX_CALLER_ID", "+15551230000")
    monkeypatch.setenv("AGENT_NAME", "usan-agent")
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
        asyncio.run(test_engine.dispose())
        get_settings.cache_clear()
