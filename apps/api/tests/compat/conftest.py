"""Shared fixtures for the compat test sub-package.

The surface-coverage test (test_surface_coverage.py) only inspects route tables — it
never touches a real database.  This conftest provides the minimum Settings env-vars so
``get_settings()`` does not raise when called without the top-level conftest's
``database_url`` / ``client`` fixtures.

Fixtures that require a live DB (``mock_dispatch``, ``allow_quiet_hours``,
``seeded_call``) compose with the top-level ``compat_client`` / ``compat_headers``
fixtures; they are used by the contract-freeze tests (T047).
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import dialer, livekit_dispatch, quiet_hours
from usan_api.compat.voice_map import to_retell_voice_id
from usan_api.schemas.voice_catalog import VOICE_CATALOG
from usan_api.settings import get_settings

RETELL_VOICE = to_retell_voice_id(VOICE_CATALOG[0].cartesia_voice_id)


@pytest.fixture(autouse=True)
def _compat_minimal_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    """Set the minimum required env-vars for Settings validation.

    Values are obviously fake — they are only used for route-inspection tests that
    never connect to any external service.  Tests that need a real DB must use the
    top-level ``client`` / ``compat_client`` fixtures instead, which supply a live
    Postgres URL and override these.
    """
    # Clear the lru_cache first so the new env values are picked up even if another
    # test in a different module already populated the cache with different values.
    get_settings.cache_clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql://usan:usan@localhost:5432/usan")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)
    yield
    get_settings.cache_clear()


@pytest.fixture
def mock_dispatch(monkeypatch):
    """Stub out livekit_dispatch.dispatch_agent and dialer.schedule_dial.

    Returns the AsyncMock so callers can assert it was awaited.
    """
    agent = AsyncMock()
    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", agent)
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)
    return agent


@pytest.fixture
def allow_quiet_hours(monkeypatch):
    """Neutralize the quiet-hours gate so happy-path tests are wall-clock-deterministic."""
    monkeypatch.setattr(quiet_hours, "next_allowed", lambda dt, tz, **k: dt)


def create_call(compat_client, compat_headers, **overrides):
    """Helper (not a fixture) — POST /v2/create-phone-call with sensible defaults.

    Callers may pass keyword overrides to test specific request fields.
    """
    body = {"from_number": "+15551230000", "to_number": "+15557654321"}
    body.update(overrides)
    return compat_client.post("/v2/create-phone-call", json=body, headers=compat_headers)


def _published_agent_id(client, headers: dict) -> str:
    """Create a compat LLM + agent + publish it so agent_id and agent_version are non-null.

    Required for oracle-conformant Call objects: V2CallBase marks agent_id and agent_version
    as required (not nullable), so the call must be linked to a published profile.
    """
    llm = client.post(
        "/create-retell-llm",
        json={"start_speaker": "agent", "general_prompt": "hi"},
        headers=headers,
    ).json()
    agent = client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "voice_id": RETELL_VOICE,
            "agent_name": "Seed Agent",
        },
        headers=headers,
    ).json()
    agent_id = agent["agent_id"]
    client.post(
        f"/publish-agent-version/{agent_id}",
        json={"version": 1},
        headers=headers,
    )
    return agent_id


@pytest.fixture
def seeded_call(compat_client, compat_headers, mock_dispatch, allow_quiet_hours) -> str:
    """Seed one call via the compat API and return its ``call_id``.

    Creates a published agent first so the serialized Call has non-null agent_id and
    agent_version — required fields in V2CallBase (oracle contract).
    Consumed by Tasks 6-8, 14, 15 contract-freeze tests that need an existing call in the DB.
    """
    agent_id = _published_agent_id(compat_client, compat_headers)
    return create_call(compat_client, compat_headers, override_agent_id=agent_id).json()["call_id"]


@pytest.fixture
def web_agent_id(compat_client, compat_headers) -> str:
    """A published agent_id usable as create-web-call's agent_id."""
    return _published_agent_id(compat_client, compat_headers)


@pytest.fixture
def mock_web_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the LiveKit web dispatch so the freeze tests place no real call."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr("usan_api.livekit_dispatch.dispatch_web_agent", AsyncMock())


@pytest.fixture
def published_default_agent(compat_client, compat_headers, async_database_url) -> str:
    """Publish an agent (via the compat API) and mark it the ACTIVE default OUTBOUND
    profile via a direct superuser UPDATE, so a no-override create-phone-call resolves it.
    Returns the compat agent_id.

    NOTE: an identical fixture exists in tests/conftest.py; pytest visibility scoping
    requires the duplication — keep the raw SQL in sync between both copies.
    """
    agent_id = _published_agent_id(compat_client, compat_headers)

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
