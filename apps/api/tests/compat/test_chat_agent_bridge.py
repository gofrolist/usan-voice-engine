"""Phase 4c-1: chat_agent_bridge create/serialize round-trip + channel stamping."""

from __future__ import annotations

import pytest
from sqlalchemy import event, text

from usan_api.compat import agent_bridge, chat_agent_bridge, ids
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.chat_agents import ChatAgentCreateRequest, ChatAgentUpdateRequest
from usan_api.compat.schemas.retell_llm import CreateRetellLlmRequest
from usan_api.settings import Settings
from usan_api.tenant_context import set_tenant_context


def _settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql://u:p@host/db",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "JWT_SIGNING_KEY": "s" * 32,
        "OPERATOR_API_KEY": "o" * 32,
    }
    base.update(overrides)
    return Settings(**base)


async def _org(app_session):
    """Resolve an org id and install an after_begin listener that re-applies the RLS
    context after each COMMIT (set_config is_local=true is transaction-scoped; functions
    like create_response_engine commit internally then call db.refresh — without the
    re-apply the post-commit SELECT runs context-free and RLS hides the row)."""
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    org_str = str(org_id)

    def _reapply(_session, _transaction, connection) -> None:
        connection.execute(
            text("SELECT set_config('app.current_org', :org, true)"), {"org": org_str}
        )

    event.listen(app_session.sync_session, "after_begin", _reapply)
    await set_tenant_context(app_session, org_id)
    return org_id


@pytest.mark.asyncio
async def test_create_chat_agent_stamps_channel_and_serializes(app_session):
    await _org(app_session)
    settings = _settings()
    llm = await agent_bridge.create_response_engine(
        app_session, settings, CreateRetellLlmRequest(general_prompt="hi")
    )
    # after_begin re-applies RLS context after create_response_engine's commit
    body = ChatAgentCreateRequest(
        response_engine={"type": "retell-llm", "llm_id": ids.encode_llm_id(llm.id)},
        agent_name="Chat Bot",
    )
    profile = await chat_agent_bridge.create_chat_agent(app_session, settings, body)
    assert profile.channel == "chat"
    payload = chat_agent_bridge.serialize_chat_agent(profile).model_dump(exclude_none=True)
    assert payload["agent_id"] == ids.encode_agent_id(profile.id)
    assert payload["response_engine"]["type"] == "retell-llm"
    assert payload["is_published"] is True


@pytest.mark.asyncio
async def test_update_chat_agent_restamps_channel(app_session):
    await _org(app_session)
    settings = _settings()
    llm = await agent_bridge.create_response_engine(
        app_session, settings, CreateRetellLlmRequest(general_prompt="hi")
    )
    body = ChatAgentCreateRequest(
        response_engine={"type": "retell-llm", "llm_id": ids.encode_llm_id(llm.id)},
        agent_name="Chat Bot",
    )
    profile = await chat_agent_bridge.create_chat_agent(app_session, settings, body)
    assert profile.channel == "chat"

    update_body = ChatAgentUpdateRequest(agent_name="Renamed Bot")
    updated = await chat_agent_bridge.update_chat_agent(
        app_session, settings, ids.encode_agent_id(profile.id), update_body
    )
    assert updated.channel == "chat"


@pytest.mark.asyncio
async def test_create_chat_agent_rejects_non_retell_llm(app_session):
    await _org(app_session)
    body = ChatAgentCreateRequest(response_engine={"type": "custom-llm", "llm_id": None})
    with pytest.raises(CompatError) as exc:
        await chat_agent_bridge.create_chat_agent(app_session, _settings(), body)
    assert exc.value.status_code == 422
