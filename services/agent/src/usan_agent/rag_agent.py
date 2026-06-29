"""Voice-RAG agent (Phase 5c).

A thin Agent subclass that, on each completed user turn, fetches knowledge-base context
from the API (server re-derives org + kb_ids — we send only call_id + query) and injects it
into the turn's chat context before the LLM responds. Ephemeral: the context is added to the
turn context for this generation only, not persisted into running history.

Everything is gated and exception-guarded: an exception raised in on_user_turn_completed
would ABORT the turn, so a retrieval failure must never escape this method.

The enabled flag is derived from ``settings.kb_retrieval_voice_enabled`` — pass ``settings``
to control it; there is no separate ``enabled`` constructor parameter.
"""

from __future__ import annotations

from typing import Any

from livekit.agents import llm
from livekit.agents.voice import Agent
from loguru import logger

from usan_agent import api_client
from usan_agent.settings import Settings

_CONTEXT_PREFIX = "Knowledge base context:\n"
_CONTEXT_SUFFIX = "\n\nUse the above context to answer when relevant."


class RagAgent(Agent):
    def __init__(
        self,
        *,
        call_id: str | None = None,
        kb_ids: list[str] | None = None,
        settings: Settings | None = None,
        **agent_kwargs: Any,
    ) -> None:
        super().__init__(**agent_kwargs)
        # kb_ids is a LOCAL GATE only (skip the round-trip when nothing is bound); never sent.
        self._kb_call_id = call_id
        self._kb_ids = kb_ids or []
        self._kb_settings = settings
        # Derive enabled from settings — no separate enabled param avoids call-site duplication.
        self._kb_enabled = bool(settings and settings.kb_retrieval_voice_enabled)

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        try:
            if not (
                self._kb_enabled
                and self._kb_call_id
                and self._kb_ids
                and self._kb_settings is not None
            ):
                return
            query = new_message.text_content
            if not query or not query.strip():
                return
            context = await api_client.retrieve_kb_context(
                self._kb_call_id, self._kb_settings, query
            )
            if context:
                turn_ctx.add_message(
                    role="system", content=_CONTEXT_PREFIX + context + _CONTEXT_SUFFIX
                )
        except Exception as exc:  # an exception here would abort the turn — swallow it
            logger.bind(err=type(exc).__name__).warning(
                "voice kb retrieval hook failed; continuing without context"
            )
