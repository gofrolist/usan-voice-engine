"""Phase 6-runtime-chat: the flow flag defaults off and the cursor column exists."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.models import ChatSession
from usan_api.settings import get_settings

TEST_SECRET = "a" * 32


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_flow_runtime_flag_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same construction idiom as tests/test_settings.py: get_settings() requires these
    # env vars to resolve; the point here is that FLOW_RUNTIME_ENABLED is absent and the
    # flag still defaults to False.
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", TEST_SECRET)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)
    monkeypatch.delenv("FLOW_RUNTIME_ENABLED", raising=False)

    assert get_settings().flow_runtime_enabled is False


def test_chat_session_has_flow_cursor_attribute() -> None:
    assert hasattr(ChatSession, "flow_current_node_id")


def test_migration_adds_flow_cursor_column(async_database_url: str) -> None:
    async def _check() -> list[str]:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                rows = await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'chat_sessions' "
                        "AND column_name = 'flow_current_node_id'"
                    )
                )
                return [r[0] for r in rows]
        finally:
            await engine.dispose()

    assert asyncio.run(_check()) == ["flow_current_node_id"]
