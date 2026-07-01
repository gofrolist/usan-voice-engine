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

_FLOW_TURN_WINDOW = 20


def _turns_for_flow(
    turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
) -> list[dict[str, str]]:
    """Build the recent conversation window for the classifier. LiveKit 'assistant' turns map
    to 'agent' (so the server treats them as model turns); system turns (e.g. injected kb
    context) are excluded. The just-completed user message is appended last."""
    turns: list[dict[str, str]] = []
    for item in turn_ctx.items:
        if not isinstance(item, llm.ChatMessage):
            continue
        if item.role not in ("user", "assistant"):
            continue
        text = item.text_content
        if not text:
            continue
        turns.append({"role": "agent" if item.role == "assistant" else "user", "content": text})
    new_text = new_message.text_content
    if new_text:
        turns.append({"role": "user", "content": new_text})
    return turns[-_FLOW_TURN_WINDOW:]


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
        # Phase 6-runtime-voice: derive the flow flag from settings (like _kb_enabled). The
        # cursor is held here (agent owns the call); the endpoint is stateless. Once an advance
        # definitively reports the call is not flow-bound, latch off (no per-turn calls on the
        # common non-flow path). A transient failure (None) does NOT latch.
        self._flow_enabled = bool(settings and settings.flow_runtime_voice_enabled)
        self._flow_node_id: str | None = None
        self._flow_latched_off = False

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        await self._maybe_advance_flow(turn_ctx, new_message)
        await self._maybe_inject_kb(turn_ctx, new_message)

    async def _maybe_advance_flow(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        try:
            if not (self._flow_enabled and self._kb_call_id) or self._flow_latched_off:
                return
            if self._kb_settings is None:
                return
            turns = _turns_for_flow(turn_ctx, new_message)
            result = await api_client.flow_advance(
                self._kb_call_id,
                self._kb_settings,
                current_node_id=self._flow_node_id,
                turns=turns,
            )
            if result is None:
                return  # transient failure: retry next turn, no latch, stay on current node
            if not result.get("bound"):
                self._flow_latched_off = True
                return
            instruction = result.get("instruction")
            if isinstance(instruction, str) and instruction:
                await self.update_instructions(instruction)
            node_id = result.get("node_id")
            if isinstance(node_id, str) and node_id:
                self._flow_node_id = node_id
        except Exception as exc:  # a failure here must never abort the turn
            logger.bind(err=type(exc).__name__).warning(
                "voice flow advance hook failed; staying on current node"
            )

    async def _maybe_inject_kb(
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
