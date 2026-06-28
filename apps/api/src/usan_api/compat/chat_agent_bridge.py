"""Bridge the RetellAI chat-agent contract onto native AgentProfile (Phase 4c-1).

A chat agent is an AgentProfile with channel='chat' — the SAME overlay as a voice agent
(agent_bridge), minus the voice fields. ``create-chat-agent`` binds the agent half onto the
profile its ``response_engine.llm_id`` points at (a prior ``create-retell-llm``), stamps
channel='chat', and publishes. The submitted ChatAgentRequest config is echoed verbatim via
compat_extras['chat_agent'] (persisted-not-honored; the analysis config is consumed by 4c-2).
Reuses agent_bridge's overlay helpers so the voice/chat overlays stay in lockstep.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import agent_bridge, ids
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.chat_agents import (
    ChatAgentCreateRequest,
    ChatAgentResponse,
    ChatAgentUpdateRequest,
)
from usan_api.compat.serialization import to_ms
from usan_api.db.models import AgentProfile, AgentProfileVersion
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories.agent_profiles import ProfileInUseError
from usan_api.settings import Settings

_CHANNEL: Literal["chat"] = "chat"
_EXTRAS_HALF = "chat_agent"


def _require_retell_llm(body: ChatAgentCreateRequest | ChatAgentUpdateRequest) -> uuid.UUID:
    engine = body.response_engine
    if engine is None or engine.type != "retell-llm" or not engine.llm_id:
        raise CompatError(422, "response_engine must be a retell-llm with an llm_id")
    return ids.decode_llm_id(engine.llm_id)


async def create_chat_agent(
    db: AsyncSession, settings: Settings, body: ChatAgentCreateRequest
) -> AgentProfile:
    """create-chat-agent: bind the chat config onto the response-engine's profile, mark it
    channel='chat', publish. Never sets the call-plane default flags."""
    profile = await agent_bridge._load_active(db, _require_retell_llm(body), kind="response engine")
    config = agent_bridge._config_dict(profile)
    agent_bridge._merge_extras(config, _EXTRAS_HALF, body.model_dump())
    agent_bridge._validate_config(config)
    updated = await agent_profiles_repo.update_draft(
        db, profile.id, config=config, description=None, actor_email=agent_bridge._ACTOR
    )
    if updated is None:  # pragma: no cover - loaded active above
        raise CompatError(404, "response engine not found")
    updated.channel = _CHANNEL
    if body.agent_name:
        updated.name = await agent_bridge._unique_name(db, body.agent_name, exclude_id=updated.id)
    await agent_bridge._publish_and_commit(db, profile.id, note="compat create-chat-agent")
    await db.refresh(updated)
    return updated


async def get_chat_agent(db: AsyncSession, agent_id: str) -> AgentProfile:
    return await agent_bridge._load_active(
        db, ids.decode_agent_id(agent_id), kind="chat agent", expected_channel=_CHANNEL
    )


async def list_chat_agents(db: AsyncSession) -> list[AgentProfile]:
    return await agent_bridge.list_agent_profiles(db, channel=_CHANNEL)


async def list_chat_agent_versions(
    db: AsyncSession, agent_id: str
) -> tuple[AgentProfile, list[AgentProfileVersion]]:
    profile = await agent_bridge._load_active(
        db, ids.decode_agent_id(agent_id), kind="chat agent", expected_channel=_CHANNEL
    )
    versions = await agent_profiles_repo.list_versions(db, profile.id)
    return profile, versions


async def update_chat_agent(
    db: AsyncSession, settings: Settings, agent_id: str, body: ChatAgentUpdateRequest
) -> AgentProfile:
    profile = await agent_bridge._load_active(
        db, ids.decode_agent_id(agent_id), kind="chat agent", expected_channel=_CHANNEL
    )
    config = agent_bridge._config_dict(profile)
    agent_bridge._merge_extras(config, _EXTRAS_HALF, body.model_dump(exclude_none=True))
    agent_bridge._validate_config(config)
    updated = await agent_profiles_repo.update_draft(
        db, profile.id, config=config, description=None, actor_email=agent_bridge._ACTOR
    )
    if updated is None:  # pragma: no cover
        raise CompatError(404, "chat agent not found")
    if body.agent_name:
        updated.name = await agent_bridge._unique_name(db, body.agent_name, exclude_id=updated.id)
    await agent_bridge._publish_and_commit(db, profile.id, note="compat update-chat-agent")
    await db.refresh(updated)
    return updated


async def delete_chat_agent(db: AsyncSession, agent_id: str) -> None:
    profile = await agent_bridge._load_active(
        db, ids.decode_agent_id(agent_id), kind="chat agent", expected_channel=_CHANNEL
    )
    try:
        archived = await agent_profiles_repo.archive_profile(db, profile.id)
    except ProfileInUseError as exc:
        raise CompatError(409, str(exc)) from exc
    if archived is None:  # pragma: no cover
        raise CompatError(404, "chat agent not found")
    await db.commit()


async def publish_chat_agent(db: AsyncSession, agent_id: str) -> None:
    """publish-chat-agent (deprecated, 200 no body): publish the latest draft."""
    profile = await agent_bridge._load_active(
        db, ids.decode_agent_id(agent_id), kind="chat agent", expected_channel=_CHANNEL
    )
    version = await agent_profiles_repo.publish(
        db, profile.id, note="compat publish-chat-agent", actor_email=agent_bridge._ACTOR
    )
    if version is None:  # pragma: no cover
        raise CompatError(404, "chat agent not found")
    await db.commit()


def serialize_chat_agent(profile: AgentProfile) -> ChatAgentResponse:
    config = profile.draft_config or {}
    extras = (config.get(agent_bridge._EXTRAS_KEY) or {}).get(_EXTRAS_HALF) or {}
    data: dict[str, Any] = dict(extras)  # echo the CRM's submitted chat config
    published = profile.published_version
    data.update(
        {
            "agent_id": ids.encode_agent_id(profile.id),
            "agent_name": profile.name,
            "response_engine": {
                "type": "retell-llm",
                "llm_id": ids.encode_llm_id(profile.id),
                "version": published or 0,
            },
            "version": published or 0,
            "is_published": published is not None,
            "last_modification_timestamp": to_ms(profile.updated_at),
        }
    )
    return ChatAgentResponse(**data)


def serialize_chat_agent_version(
    profile: AgentProfile, version_row: AgentProfileVersion
) -> ChatAgentResponse:
    base = serialize_chat_agent(profile)
    return base.model_copy(
        update={
            "version": version_row.version,
            "last_modification_timestamp": to_ms(version_row.published_at),
            "is_published": True,
        }
    )
