from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.compat.errors import CompatError
from usan_api.compat.ids import encode_agent_id
from usan_api.compat.playground_service import run_playground_completion
from usan_api.compat.schemas.playground import PlaygroundCompletionRequest
from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, AgentProfileVersion
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context
from usan_api.vertex_test import VertexTurn

_CONFIG = DEFAULT_AGENT_CONFIG.model_copy(
    update={
        "prompts": DEFAULT_AGENT_CONFIG.prompts.model_copy(
            update={"system_prompt": "You are {{name}}. Be brief."}
        )
    }
).model_dump()


async def _seed_published_profile(db) -> AgentProfile:
    profile = AgentProfile(
        name=f"PG Agent {uuid.uuid4().hex[:8]}",
        draft_config=_CONFIG,
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(profile)
    await db.flush()
    db.add(AgentProfileVersion(profile_id=profile.id, version=1, config=_CONFIG))
    await db.flush()
    return profile


def _settings(project: str | None):
    return get_settings().model_copy(update={"gcp_project": project})


async def _current_org(db) -> uuid.UUID:
    return (await db.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()


def _super_url(app_async_database_url: str) -> str:
    """The superuser (RLS-bypassing) async DSN, derived from the usan_app one.

    Canonical idiom reused from tests/test_rls_p2_isolation.py::_super_url /
    tests/test_compat_rls_isolation.py — a raw superuser engine is required to seed a
    row directly into a *specific* org regardless of the reading session's tenant
    context, since the app-role WITH CHECK policy only allows inserts matching the
    session's own current org.
    """
    return app_async_database_url.replace("usan_app:usan_app@", "usan:usan@", 1)


async def _seed_published_profile_in_org(super_async_url: str, org_id: uuid.UUID) -> uuid.UUID:
    """Insert a published agent_profile + its version straight into ``org_id`` via the
    superuser engine (RLS bypassed), mirroring
    tests/test_rls_p2_isolation.py::_seed_profile_in_org — so the seeded row genuinely
    belongs to a different org than whatever the reading session is scoped to.
    """
    profile_id = uuid.uuid4()
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO agent_profiles "
                    "(id, organization_id, name, status, draft_config, published_version) "
                    "VALUES (:id, :org, :name, 'active', CAST(:cfg AS jsonb), 1)"
                ),
                {
                    "id": profile_id,
                    "org": org_id,
                    "name": f"PG Cross-Org Agent {uuid.uuid4().hex[:8]}",
                    "cfg": json.dumps(_CONFIG),
                },
            )
            await conn.execute(
                text(
                    "INSERT INTO agent_profile_versions (profile_id, version, config) "
                    "VALUES (:pid, 1, CAST(:cfg AS jsonb))"
                ),
                {"pid": profile_id, "cfg": json.dumps(_CONFIG)},
            )
    finally:
        await engine.dispose()
    return profile_id


async def _delete_profile(super_async_url: str, profile_id: uuid.UUID) -> None:
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM agent_profile_versions WHERE profile_id = :pid"),
                {"pid": profile_id},
            )
            await conn.execute(
                text("DELETE FROM agent_profiles WHERE id = :pid"), {"pid": profile_id}
            )
    finally:
        await engine.dispose()


async def test_happy_path_single_turn(app_session, monkeypatch) -> None:
    org = await _current_org(app_session)
    await set_tenant_context(app_session, org)
    profile = await _seed_published_profile(app_session)

    captured: dict = {}

    async def fake_turn(**kwargs):
        captured.update(kwargs)
        return VertexTurn(text="Hello!")

    monkeypatch.setattr("usan_api.compat.playground_service.run_vertex_turn", fake_turn)

    req = PlaygroundCompletionRequest(messages=[{"role": "user", "content": "hi"}])
    resp = await run_playground_completion(
        app_session,
        _settings("proj"),
        agent_id=encode_agent_id(profile.id),
        version=None,
        request=req,
    )
    assert len(resp.messages) == 1
    assert resp.messages[0].role == "agent"
    assert resp.messages[0].content == "Hello!"
    assert resp.messages[0].message_id
    assert resp.messages[0].created_timestamp > 0
    # tools always empty; last content is the user turn
    assert captured["tools"] == []
    assert captured["contents"][-1] == {"role": "user", "parts": [{"text": "hi"}]}


async def test_multi_turn_role_mapping(app_session, monkeypatch) -> None:
    org = await _current_org(app_session)
    await set_tenant_context(app_session, org)
    profile = await _seed_published_profile(app_session)
    captured: dict = {}

    async def fake_turn(**kwargs):
        captured.update(kwargs)
        return VertexTurn(text="ok")

    monkeypatch.setattr("usan_api.compat.playground_service.run_vertex_turn", fake_turn)
    req = PlaygroundCompletionRequest(
        messages=[
            {"role": "user", "content": "one"},
            {"role": "agent", "content": "two"},
            {"role": "user", "content": "three"},
        ]
    )
    await run_playground_completion(
        app_session,
        _settings("proj"),
        agent_id=encode_agent_id(profile.id),
        version=None,
        request=req,
    )
    assert [c["role"] for c in captured["contents"]] == ["user", "model", "user"]


async def test_dynamic_variables_substituted(app_session, monkeypatch) -> None:
    org = await _current_org(app_session)
    await set_tenant_context(app_session, org)
    profile = await _seed_published_profile(app_session)
    captured: dict = {}

    async def fake_turn(**kwargs):
        captured.update(kwargs)
        return VertexTurn(text="ok")

    monkeypatch.setattr("usan_api.compat.playground_service.run_vertex_turn", fake_turn)
    req = PlaygroundCompletionRequest(
        messages=[{"role": "user", "content": "hi"}], dynamic_variables={"name": "Robo"}
    )
    await run_playground_completion(
        app_session,
        _settings("proj"),
        agent_id=encode_agent_id(profile.id),
        version=None,
        request=req,
    )
    assert "Robo" in captured["system_instruction"]


async def test_advanced_fields_ignored(app_session, monkeypatch) -> None:
    org = await _current_org(app_session)
    await set_tenant_context(app_session, org)
    profile = await _seed_published_profile(app_session)

    async def fake_turn(**kwargs):
        return VertexTurn(text="only one")

    monkeypatch.setattr("usan_api.compat.playground_service.run_vertex_turn", fake_turn)
    req = PlaygroundCompletionRequest(
        messages=[{"role": "user", "content": "hi"}],
        tool_mocks=[{"tool_name": "x", "output": "y", "input_match_rule": "any"}],
        current_state="greeting",
        current_node_id="node_1",
    )
    resp = await run_playground_completion(
        app_session,
        _settings("proj"),
        agent_id=encode_agent_id(profile.id),
        version=None,
        request=req,
    )
    dumped = resp.model_dump(exclude_none=True)
    assert list(dumped.keys()) == ["messages"]
    assert len(resp.messages) == 1


async def test_unknown_agent_raises_422(app_session) -> None:
    org = await _current_org(app_session)
    await set_tenant_context(app_session, org)
    with pytest.raises(CompatError) as ei:
        await run_playground_completion(
            app_session,
            _settings("proj"),
            agent_id=encode_agent_id(uuid.uuid4()),
            version=None,
            request=PlaygroundCompletionRequest(messages=[{"role": "user", "content": "hi"}]),
        )
    assert ei.value.status_code == 422
    assert ei.value.message == "agent is not available"


async def test_malformed_agent_id_raises_422(app_session) -> None:
    org = await _current_org(app_session)
    await set_tenant_context(app_session, org)
    with pytest.raises(CompatError) as ei:
        await run_playground_completion(
            app_session,
            _settings("proj"),
            agent_id="not-an-agent-id",
            version=None,
            request=PlaygroundCompletionRequest(messages=[{"role": "user", "content": "hi"}]),
        )
    assert ei.value.status_code == 422
    assert ei.value.message == "invalid agent_id"


async def test_cross_org_agent_raises_422(app_session, app_async_database_url, two_orgs) -> None:
    """Spec §7/§9 cross-org isolation: an agent published under a DIFFERENT org must be
    indistinguishable from not-found — 422 "agent is not available", never 200 — so no
    config/PHI leaks across orgs. Runs on the real usan_app/RLS-enforcing ``app_session``
    (not a superuser session, which would bypass RLS and pass vacuously); only the seed
    of org A's row uses the superuser engine idiom (see ``_seed_published_profile_in_org``).
    """
    org_a, org_b = two_orgs
    super_url = _super_url(app_async_database_url)
    profile_id = await _seed_published_profile_in_org(super_url, org_a)
    try:
        await set_tenant_context(app_session, org_b)
        with pytest.raises(CompatError) as ei:
            await run_playground_completion(
                app_session,
                _settings("proj"),
                agent_id=encode_agent_id(profile_id),
                version=None,
                request=PlaygroundCompletionRequest(messages=[{"role": "user", "content": "hi"}]),
            )
        assert ei.value.status_code == 422
        assert ei.value.message == "agent is not available"
    finally:
        await _delete_profile(super_url, profile_id)


async def test_no_gcp_project_raises_503(app_session) -> None:
    org = await _current_org(app_session)
    await set_tenant_context(app_session, org)
    profile = await _seed_published_profile(app_session)
    with pytest.raises(CompatError) as ei:
        await run_playground_completion(
            app_session,
            _settings(None),
            agent_id=encode_agent_id(profile.id),
            version=None,
            request=PlaygroundCompletionRequest(messages=[{"role": "user", "content": "hi"}]),
        )
    assert ei.value.status_code == 503


async def test_content_less_messages_are_skipped(app_session, monkeypatch) -> None:
    org = await _current_org(app_session)
    await set_tenant_context(app_session, org)
    profile = await _seed_published_profile(app_session)
    captured: dict = {}

    async def fake_turn(**kwargs):
        captured.update(kwargs)
        return VertexTurn(text="ok")

    monkeypatch.setattr("usan_api.compat.playground_service.run_vertex_turn", fake_turn)
    req = PlaygroundCompletionRequest(
        messages=[
            {"role": "user", "content": "one"},
            {"role": "tool_call_invocation", "tool_call_id": "t1"},
            {"role": "user", "content": "two"},
        ]
    )
    await run_playground_completion(
        app_session,
        _settings("proj"),
        agent_id=encode_agent_id(profile.id),
        version=None,
        request=req,
    )
    # Verify that only the two messages with content were included in contents
    assert len(captured["contents"]) == 2
    assert captured["contents"] == [
        {"role": "user", "parts": [{"text": "one"}]},
        {"role": "user", "parts": [{"text": "two"}]},
    ]
