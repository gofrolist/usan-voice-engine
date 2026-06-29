from unittest.mock import AsyncMock

from livekit.agents import llm

from usan_agent import api_client
from usan_agent.rag_agent import RagAgent


def _settings(voice_enabled: bool = True):
    # Build a minimal real Settings. kb_retrieval_voice_enabled controls the RAG flag.
    from usan_agent.settings import Settings

    return Settings(
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="s" * 32,
        LIVEKIT_URL="ws://lk",
        CARTESIA_API_KEY="c",
        GCP_PROJECT="p",
        DEFAULT_CARTESIA_VOICE_ID="v",
        API_BASE_URL="http://api:8000",
        JWT_SIGNING_KEY="j" * 32,
        KB_RETRIEVAL_VOICE_ENABLED=voice_enabled,
    )


def _agent(*, enabled: bool, call_id: str | None = "call-1", kb_ids: tuple = ("knowledge_base_a",)):
    return RagAgent(
        call_id=call_id,
        kb_ids=list(kb_ids) if kb_ids else [],
        settings=_settings(voice_enabled=enabled),
        instructions="be kind",
    )


def _user_msg(text):
    return llm.ChatMessage(role="user", content=[text])


async def test_injects_context_on_hit(monkeypatch):
    spy = AsyncMock(return_value="KB FACT")
    monkeypatch.setattr(api_client, "retrieve_kb_context", spy)
    agent = _agent(enabled=True)
    ctx = llm.ChatContext.empty()
    await agent.on_user_turn_completed(ctx, _user_msg("how do meds work"))
    spy.assert_awaited_once()
    # only call_id + query are passed to the client (positional: call_id, settings, query)
    assert spy.await_args.args[0] == "call-1"
    assert spy.await_args.args[2] == "how do meds work"
    # at least one system message now carries the injected context
    assert any(
        getattr(m, "role", None) == "system" and "KB FACT" in (m.text_content or "")
        for m in ctx.items
        if isinstance(m, llm.ChatMessage)
    )


async def test_no_call_when_disabled(monkeypatch):
    spy = AsyncMock(return_value="KB FACT")
    monkeypatch.setattr(api_client, "retrieve_kb_context", spy)
    agent = _agent(enabled=False)
    ctx = llm.ChatContext.empty()
    await agent.on_user_turn_completed(ctx, _user_msg("q"))
    spy.assert_not_awaited()


async def test_no_call_when_no_kb_bound(monkeypatch):
    spy = AsyncMock(return_value="KB FACT")
    monkeypatch.setattr(api_client, "retrieve_kb_context", spy)
    agent = _agent(enabled=True, kb_ids=())
    await agent.on_user_turn_completed(llm.ChatContext.empty(), _user_msg("q"))
    spy.assert_not_awaited()


async def test_no_call_when_no_call_id(monkeypatch):
    spy = AsyncMock(return_value="KB FACT")
    monkeypatch.setattr(api_client, "retrieve_kb_context", spy)
    agent = _agent(enabled=True, call_id=None)
    await agent.on_user_turn_completed(llm.ChatContext.empty(), _user_msg("q"))
    spy.assert_not_awaited()


async def test_no_injection_on_empty_context(monkeypatch):
    monkeypatch.setattr(api_client, "retrieve_kb_context", AsyncMock(return_value=""))
    agent = _agent(enabled=True)
    ctx = llm.ChatContext.empty()
    await agent.on_user_turn_completed(ctx, _user_msg("q"))
    assert all(
        getattr(m, "role", None) != "system" for m in ctx.items if isinstance(m, llm.ChatMessage)
    )


async def test_retrieval_error_does_not_raise(monkeypatch):
    monkeypatch.setattr(api_client, "retrieve_kb_context", AsyncMock(side_effect=RuntimeError("x")))
    agent = _agent(enabled=True)
    # must not raise (an exception here would abort the turn)
    await agent.on_user_turn_completed(llm.ChatContext.empty(), _user_msg("q"))


async def test_no_call_on_blank_utterance(monkeypatch):
    spy = AsyncMock(return_value="KB FACT")
    monkeypatch.setattr(api_client, "retrieve_kb_context", spy)
    agent = _agent(enabled=True)
    await agent.on_user_turn_completed(llm.ChatContext.empty(), _user_msg(""))
    spy.assert_not_awaited()
