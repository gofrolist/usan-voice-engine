"""Pre-publish agent test endpoints (US5 / FR-025–FR-027, C1). Written FIRST.

``POST /v1/admin/profiles/{id}/test/llm`` runs the draft prompt against Vertex AI
(mocked here — no live provider) with STUB tools, writes NO DB rows, and makes NO
``/v1/tools/*`` call. ``POST .../test/audio`` mints a join-only short-TTL LiveKit
token and dispatches the agent with ``session_kind="test"`` (LiveKit mocked) — no
``Call`` row, no PSTN. Viewers get 403. Each invocation emits exactly ONE PHI-free
audit entry (actor + profile + kind), never the sample-var values (C1 / FR-029).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from usan_api.db.base import AdminRole


def _name() -> str:
    import uuid

    return f"profile-{uuid.uuid4().hex}"


def _new_profile(client: TestClient) -> str:
    return client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]


def _draft_config_json() -> dict:
    """A JSON-serializable copy of the server default config, for inline overrides."""
    from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG

    return DEFAULT_AGENT_CONFIG.model_dump(mode="json")


async def _seed_admin_user(async_database_url: str, email: str) -> None:
    """Seed an identity-only admin_users row (role moved to memberships, P2 / 0033)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, status, added_by) "
                    "VALUES (:e, 'active', 'test') "
                    "ON CONFLICT (email) DO NOTHING"
                ),
                {"e": email.lower()},
            )
    finally:
        await engine.dispose()


def _as_viewer(client: TestClient, async_database_url: str) -> None:
    from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
    from usan_api.settings import get_settings

    asyncio.run(_seed_admin_user(async_database_url, "viewer@example.com"))
    token = issue_session(
        "viewer@example.com",
        active_org_id=None,
        role=AdminRole.VIEWER,
        is_super_admin=False,
        acting_as=False,
        settings=get_settings(),
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)


@pytest.fixture
def vertex_admin(monkeypatch):
    """Configure GCP_PROJECT so test/llm is enabled, and stub the Vertex call.

    The stub returns a fixed assistant string and one tool call so we can assert
    the loop, the echo, and the no-DB-write invariants without a live provider.
    """
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    from usan_api.settings import get_settings

    get_settings.cache_clear()

    calls = {"n": 0, "model": None, "contents": None}

    async def _fake_run_llm(*, model, temperature, system_instruction, tools, contents):
        calls["n"] += 1
        calls["model"] = model
        calls["contents"] = contents
        # First turn: ask to call a tool; the handler feeds a stub result and loops.
        return SimpleNamespace(
            text="Hello, how are you feeling today?",
            tool_calls=[SimpleNamespace(name="get_today_meds", args={})],
        )

    monkeypatch.setattr("usan_api.routers.admin_profile_tests._run_vertex_turn", _fake_run_llm)
    return calls


# --- test/llm -------------------------------------------------------------


def test_llm_requires_session(client):
    pid = "00000000-0000-0000-0000-000000000000"
    r = client.post(
        f"/v1/admin/profiles/{pid}/test/llm",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401


def test_llm_viewer_forbidden(client, super_admin_acting_session, async_database_url, vertex_admin):
    pid = _new_profile(client)
    _as_viewer(client, async_database_url)
    r = client.post(
        f"/v1/admin/profiles/{pid}/test/llm",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 403


def test_llm_runs_vertex_with_stub_tools_no_db_rows(
    client, super_admin_acting_session, async_database_url, vertex_admin
):
    pid = _new_profile(client)
    r = client.post(
        f"/v1/admin/profiles/{pid}/test/llm",
        json={
            "messages": [{"role": "user", "content": "I'm okay"}],
            "sample_vars": {"first_name": "Synthetic"},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assistant"] == "Hello, how are you feeling today?"
    # The model's tool call is echoed to the UI (stubbed, never executed live).
    assert any(tc["name"] == "get_today_meds" for tc in body["tool_calls"])
    # The Vertex call ran with the draft's llm.model (default gemini-3.1-flash-lite).
    assert vertex_admin["model"] == "gemini-3.1-flash-lite"

    # No production rows of any kind were created by a text test.
    counts = asyncio.run(_table_counts(async_database_url))
    assert counts["calls"] == 0
    assert counts["wellness_logs"] == 0
    assert counts["medication_logs"] == 0


def test_llm_503_when_gcp_project_unset(client, super_admin_acting_session, monkeypatch):
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    from usan_api.settings import get_settings

    get_settings.cache_clear()
    pid = _new_profile(client)
    r = client.post(
        f"/v1/admin/profiles/{pid}/test/llm",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 503


def test_llm_emits_single_phi_free_audit_entry(
    client, super_admin_acting_session, async_database_url, vertex_admin
):
    pid = _new_profile(client)
    client.post(
        f"/v1/admin/profiles/{pid}/test/llm",
        json={
            "messages": [{"role": "user", "content": "secret patient note Margaret"}],
            "sample_vars": {"first_name": "Margaret"},
        },
    )
    entries = asyncio.run(_audit_for(async_database_url, "profile.test_llm"))
    assert len(entries) == 1
    detail_str = str(entries[0])
    # Actor + profile + kind only — never the sample-var value or message content.
    assert "Margaret" not in detail_str
    assert "secret patient note" not in detail_str
    assert pid in detail_str or pid in str(entries[0])


def test_llm_rejects_unsupported_model_before_vertex_call(
    client, super_admin_acting_session, async_database_url, vertex_admin
):
    """FR-014 parity: an inline config with an off-catalog llm.model is blocked with a
    field-level 422 BEFORE any Vertex call — the test path must not forward an
    unvalidated model id to the provider (security review PR #61, LOW #1)."""
    pid = _new_profile(client)
    cfg = _draft_config_json()
    cfg["llm"]["model"] = "totally-made-up-model"
    r = client.post(
        f"/v1/admin/profiles/{pid}/test/llm",
        json={"messages": [{"role": "user", "content": "hi"}], "config": cfg},
    )
    assert r.status_code == 422, r.text
    locs = {tuple(d["loc"]) for d in r.json()["detail"]}
    assert ("body", "config", "llm", "model") in locs
    # The gate ran BEFORE the provider — no Vertex turn was attempted.
    assert vertex_admin["n"] == 0


def test_llm_rejects_oversized_sample_var_value(client, super_admin_acting_session):
    """sample_vars values are bounded at the schema layer: an over-long value is a
    422 (not silently truncated post-parse), so the validated payload size is capped
    (security review PR #61, LOW #2). Fails at body validation — no provider needed."""
    pid = _new_profile(client)
    r = client.post(
        f"/v1/admin/profiles/{pid}/test/llm",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "sample_vars": {"first_name": "x" * 2001},
        },
    )
    assert r.status_code == 422, r.text


# --- test/audio -----------------------------------------------------------


@pytest.fixture
def mock_livekit(monkeypatch):
    """Stub the LiveKit dispatch so test/audio creates a room + dispatch without a server."""
    captured = {}

    fake = MagicMock()
    fake.agent_dispatch.create_dispatch = AsyncMock()
    fake.room.create_room = AsyncMock()
    fake.__aenter__ = AsyncMock(return_value=fake)
    fake.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr("usan_api.livekit_dispatch.build_livekit_api", lambda s: fake)

    captured["fake"] = fake
    return captured


def test_audio_viewer_forbidden(
    client, super_admin_acting_session, async_database_url, mock_livekit
):
    pid = _new_profile(client)
    _as_viewer(client, async_database_url)
    r = client.post(f"/v1/admin/profiles/{pid}/test/audio", json={"sample_vars": {}})
    assert r.status_code == 403


def test_audio_mints_join_token_and_dispatches_test_session(
    client, super_admin_acting_session, async_database_url, mock_livekit
):
    import jwt as pyjwt

    pid = _new_profile(client)
    r = client.post(
        f"/v1/admin/profiles/{pid}/test/audio",
        json={"sample_vars": {"first_name": "Synthetic"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["room"].startswith("usan-test-")
    assert body["url"].startswith("ws")
    # The token is a join-only LiveKit JWT for exactly this throwaway room.
    claims = pyjwt.decode(
        body["token"], "a" * 32, algorithms=["HS256"], options={"verify_aud": False}
    )
    grants = claims.get("video", {})
    assert grants.get("room") == body["room"]
    assert grants.get("roomJoin") is True
    assert grants.get("canPublish") is True
    assert grants.get("canSubscribe") is True
    # Short TTL (<= ~15 min) so a leaked token cannot linger (LiveKit sets nbf/exp).
    assert claims["exp"] - claims["nbf"] <= 16 * 60

    # The agent was dispatched in test mode with the draft config + sample vars inline.
    import json as _json

    fake = mock_livekit["fake"]
    fake.agent_dispatch.create_dispatch.assert_awaited_once()
    req = fake.agent_dispatch.create_dispatch.await_args.args[0]
    assert req.room == body["room"]
    meta = _json.loads(req.metadata)
    assert meta["session_kind"] == "test"
    assert meta["test_config"] is not None
    assert meta["dynamic_vars"]["first_name"] == "Synthetic"

    # No Call row was created (no PSTN, no production record).
    counts = asyncio.run(_table_counts(async_database_url))
    assert counts["calls"] == 0


def test_audio_rejects_unsupported_voice_before_dispatch(
    client, super_admin_acting_session, async_database_url, mock_livekit
):
    """FR-014 parity: an inline config with an off-catalog voice is blocked with a
    field-level 422 BEFORE the agent is dispatched (security review PR #61, LOW #1)."""
    pid = _new_profile(client)
    cfg = _draft_config_json()
    cfg["voice"]["cartesia_voice_id"] = "not-a-real-voice"
    r = client.post(
        f"/v1/admin/profiles/{pid}/test/audio",
        json={"sample_vars": {}, "config": cfg},
    )
    assert r.status_code == 422, r.text
    locs = {tuple(d["loc"]) for d in r.json()["detail"]}
    assert ("body", "config", "voice", "cartesia_voice_id") in locs
    # The gate ran BEFORE dispatch — no agent/room was created.
    mock_livekit["fake"].agent_dispatch.create_dispatch.assert_not_awaited()


def test_audio_emits_single_phi_free_audit_entry(
    client, super_admin_acting_session, async_database_url, mock_livekit
):
    pid = _new_profile(client)
    client.post(
        f"/v1/admin/profiles/{pid}/test/audio",
        json={"sample_vars": {"first_name": "Margaret"}},
    )
    entries = asyncio.run(_audit_for(async_database_url, "profile.test_audio"))
    assert len(entries) == 1
    assert "Margaret" not in str(entries[0])


# --- helpers --------------------------------------------------------------


async def _table_counts(async_database_url: str) -> dict[str, int]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(async_database_url, poolclass=NullPool)
    out: dict[str, int] = {}
    try:
        async with engine.connect() as conn:
            for table in ("calls", "wellness_logs", "medication_logs"):
                res = await conn.execute(text(f"SELECT count(*) FROM {table}"))
                out[table] = int(res.scalar_one())
    finally:
        await engine.dispose()
    return out


async def _audit_for(async_database_url: str, action: str) -> list[dict]:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    "SELECT actor_email, action, entity_id, detail FROM admin_audit_log "
                    "WHERE action = :a"
                ),
                {"a": action},
            )
            return [dict(r._mapping) for r in res]
    finally:
        await engine.dispose()
