from __future__ import annotations

from unittest.mock import AsyncMock

from livekit.agents import llm

from usan_agent import api_client
from usan_agent.rag_agent import RagAgent
from usan_agent.settings import Settings


def _settings(*, flow: bool, kb: bool = False) -> Settings:
    return Settings(
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="s" * 32,
        LIVEKIT_URL="wss://example.com",
        CARTESIA_API_KEY="c",
        GCP_PROJECT="proj",
        DEFAULT_CARTESIA_VOICE_ID="v",
        API_BASE_URL="https://api.example.com",
        JWT_SIGNING_KEY="j" * 32,
        FLOW_RUNTIME_VOICE_ENABLED=flow,
        KB_RETRIEVAL_VOICE_ENABLED=kb,
    )


def _agent(settings: Settings, **kw) -> RagAgent:
    return RagAgent(instructions="base", call_id="c-1", settings=settings, **kw)


def _user_msg(text: str) -> llm.ChatMessage:
    return llm.ChatMessage(role="user", content=[text])


async def test_flow_off_never_calls_advance(monkeypatch) -> None:
    spy = AsyncMock()
    monkeypatch.setattr(api_client, "flow_advance", spy)
    monkeypatch.setattr(api_client, "retrieve_kb_context", AsyncMock(return_value=""))
    agent = _agent(_settings(flow=False))
    await agent.on_user_turn_completed(llm.ChatContext.empty(), _user_msg("hi"))
    spy.assert_not_awaited()


async def test_bound_steers_instruction_and_advances_cursor(monkeypatch) -> None:
    spy = AsyncMock(
        return_value={
            "bound": True,
            "node_id": "n2",
            "instruction": "Ask about meds",
            "is_end": False,
        }
    )
    monkeypatch.setattr(api_client, "flow_advance", spy)
    monkeypatch.setattr(api_client, "retrieve_kb_context", AsyncMock(return_value=""))
    update = AsyncMock()
    agent = _agent(_settings(flow=True))
    monkeypatch.setattr(agent, "update_instructions", update)
    await agent.on_user_turn_completed(llm.ChatContext.empty(), _user_msg("hello"))
    update.assert_awaited_once_with("Ask about meds")
    assert agent._flow_node_id == "n2"


async def test_unbound_latches_off_after_one_call(monkeypatch) -> None:
    spy = AsyncMock(return_value={"bound": False})
    monkeypatch.setattr(api_client, "flow_advance", spy)
    monkeypatch.setattr(api_client, "retrieve_kb_context", AsyncMock(return_value=""))
    agent = _agent(_settings(flow=True))
    ctx = llm.ChatContext.empty()
    await agent.on_user_turn_completed(ctx, _user_msg("a"))
    await agent.on_user_turn_completed(ctx, _user_msg("b"))
    assert spy.await_count == 1  # latched off after the first bound=False


async def test_advance_failure_does_not_latch_or_raise(monkeypatch) -> None:
    spy = AsyncMock(return_value=None)  # transient failure
    monkeypatch.setattr(api_client, "flow_advance", spy)
    monkeypatch.setattr(api_client, "retrieve_kb_context", AsyncMock(return_value=""))
    update = AsyncMock()
    agent = _agent(_settings(flow=True))
    monkeypatch.setattr(agent, "update_instructions", update)
    ctx = llm.ChatContext.empty()
    await agent.on_user_turn_completed(ctx, _user_msg("a"))
    await agent.on_user_turn_completed(ctx, _user_msg("b"))
    assert spy.await_count == 2  # retried; no latch
    update.assert_not_awaited()
    assert agent._flow_node_id is None


async def test_kb_hook_still_runs_alongside_flow(monkeypatch) -> None:
    monkeypatch.setattr(api_client, "flow_advance", AsyncMock(return_value={"bound": False}))
    kb = AsyncMock(return_value="KB FACT")
    monkeypatch.setattr(api_client, "retrieve_kb_context", kb)
    agent = _agent(_settings(flow=True, kb=True), kb_ids=["kb_1"])
    await agent.on_user_turn_completed(llm.ChatContext.empty(), _user_msg("how do meds work"))
    kb.assert_awaited_once()
