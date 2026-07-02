"""Service for the RetellAI-compat agent-playground-completion op (Phase 7 slice 1).

Stateless single text turn: resolve the org-scoped published AgentConfig, build a
system prompt, run one Vertex turn, return one agent message. Nothing persisted.
See docs/superpowers/specs/2026-07-01-retell-parity-phase7-playground-design.md.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.playground import (
    PlaygroundCompletionRequest,
    PlaygroundCompletionResponse,
    PlaygroundMessageOut,
)
from usan_api.prompt_substitution import build_vars, substitute
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.schemas.agent_config import AgentConfig
from usan_api.settings import Settings
from usan_api.vertex_test import run_vertex_turn


async def run_playground_completion(
    db: AsyncSession,
    settings: Settings,
    *,
    agent_id: str,
    version: str | None,
    request: PlaygroundCompletionRequest,
) -> PlaygroundCompletionResponse:
    # `version` is accepted and ignored — the currently-published config is served
    # (matches get-chat-agent). Malformed id → CompatError(422) inside the codec.
    profile_id = ids.decode_agent_id(agent_id)
    profile = await agent_profiles_repo.get_profile(db, profile_id)
    published = (
        await agent_profiles_repo.get_published_config(db, profile) if profile is not None else None
    )
    if published is None:
        # unknown / cross-org (RLS-filtered) / unpublished — no 404 for this op.
        raise CompatError(422, "agent is not available")
    cfg = AgentConfig.model_validate(published.config or {})

    values = build_vars({}, request.dynamic_variables or {}, timezone="", now=datetime.now(UTC))
    system_instruction = substitute(cfg.prompts.system_prompt, values)

    # Only spoken turns reach the model: map agent->model / user->user. The other
    # ChatMessageInput oneOf variants (injected, sms, tool_call_*, transitions) carry
    # content but are "not spoken by either party" per the oracle — folding them into a
    # user turn would misattribute injected/tool context as caller speech, so skip them.
    contents = [
        {"role": "model" if m.role == "agent" else "user", "parts": [{"text": m.content}]}
        for m in request.messages
        if m.role in ("agent", "user") and m.content
    ]

    if not settings.gcp_project:
        raise CompatError(503, "playground completion unavailable")

    try:
        turn = await run_vertex_turn(
            model=cfg.llm.model,
            temperature=cfg.llm.temperature,
            system_instruction=system_instruction,
            tools=[],
            contents=contents,
            settings=settings,
        )
    except CompatError:
        raise
    except Exception as exc:  # noqa: BLE001 — PHI/secret-safe: type name only.
        logger.bind(err=type(exc).__name__).error("playground completion failed")
        raise CompatError(502, "playground completion failed") from None

    return PlaygroundCompletionResponse(
        messages=[
            PlaygroundMessageOut(
                message_id=str(uuid.uuid4()),
                content=turn.text,
                created_timestamp=int(time.time() * 1000),
            )
        ]
    )
