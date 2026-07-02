from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

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
