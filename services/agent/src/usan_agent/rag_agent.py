"""Voice-RAG + flow-steering agent (Phase 5c, extended Phase 6-runtime-voice).

A thin Agent subclass that hooks ``on_user_turn_completed`` to do two independent things,
each behind its own settings flag:

1. KB context injection (Phase 5c): fetches knowledge-base context from the API (server
   re-derives org + kb_ids — we send only call_id + query) and injects it into the turn's
   chat context before the LLM responds. Ephemeral: the context is added to the turn
   context for this generation only, not persisted into running history. Gated by
   ``settings.kb_retrieval_voice_enabled``.

2. Conversation-flow steering (Phase 6-runtime-voice): on each completed user turn, calls
   the stateless flow-advance endpoint with the agent-held cursor (an opaque,
   flow-qualified token the agent never interprets) and a recent turn window, then applies
   the returned instruction via ``update_instructions`` and stores the new cursor. The
   agent latches off (no more per-turn calls on the common non-flow path) only if the very
   first advance reports the call is not flow-bound; once a flow has ever bound, a later
   transient ``bound=False`` is treated as transient (stay on the current cursor, retry
   next turn, no latch) — see ``_flow_ever_bound``. Gated by
   ``settings.flow_runtime_voice_enabled``.

Both hooks are independently exception-guarded: an exception raised in
on_user_turn_completed would ABORT the turn, so a failure in either hook must never
escape that method.

Both enabled flags are derived from the ``settings`` constructor parameter — pass
``settings`` to control them; there are no separate ``enabled`` constructor parameters.
"""

from __future__ import annotations

import asyncio
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
        self._flow_cursor: str | None = None
        self._flow_latched_off = False
        self._flow_ever_bound = False

    @property
    def flow_active(self) -> bool:
        """True once a conversation flow is actively steering this call.

        Used by the worker's dynamic-vars receiver to decide whether the flow (rather
        than a mid-call variable update) owns the system prompt.
        """
        return self._flow_enabled and not self._flow_latched_off and self._flow_ever_bound

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        # The two hooks target independent state (flow calls update_instructions; kb calls
        # turn_ctx.add_message) and each swallows its own exceptions, so running them
        # concurrently is safe and shaves one hook's latency off the turn.
        await asyncio.gather(
            self._maybe_advance_flow(turn_ctx, new_message),
            self._maybe_inject_kb(turn_ctx, new_message),
        )

    async def _maybe_advance_flow(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        try:
            if not (self._flow_enabled and self._kb_call_id) or self._flow_latched_off:
                return
            # _flow_enabled is only True when settings was provided at construction time, so
            # this can never actually be None here; narrow the type for mypy.
            assert self._kb_settings is not None
            turns = _turns_for_flow(turn_ctx, new_message)
            result = await api_client.flow_advance(
                self._kb_call_id,
                self._kb_settings,
                cursor=self._flow_cursor,
                turns=turns,
            )
            if result is None:
                return  # transient failure: retry next turn, no latch, stay on current cursor
            if not result.get("bound"):
                # A bound=False AFTER we've seen bound=true is treated as transient (stay on
                # the current cursor, retry next turn). Only latch off if the call has never
                # bound — that's the definitive "this call has no flow" signal.
                if not self._flow_ever_bound:
                    self._flow_latched_off = True
                return
            self._flow_ever_bound = True
            instruction = result.get("instruction")
            if isinstance(instruction, str) and instruction:
                await self.update_instructions(instruction)
            cursor = result.get("cursor")
            if isinstance(cursor, str) and cursor:
                self._flow_cursor = cursor
        except Exception as exc:  # a failure here must never abort the turn
            logger.bind(err=type(exc).__name__).warning(
                "voice flow advance hook failed; staying on current cursor"
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
