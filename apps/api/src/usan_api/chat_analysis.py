"""Post-chat analysis pipeline (Phase 4c-2 / rerun-chat-analysis).

One Vertex turn over the chat transcript produces ``chat_summary`` + ``user_sentiment`` +
``chat_successful`` (the oracle ChatAnalysis fields). Mirrors ``summarization.py`` for the
chat channel: Vertex via ``vertexai=True`` + ADC ONLY (Constitution II PHI containment),
defensive JSON parse, sentiment coerced to the closed enum, and a try/except that logs the
exception TYPE only and never raises — so the inline rerun endpoint always returns 201.

``analyze_chat_with`` is the reusable core: gated on ``chat_analysis_enabled`` + a configured
``gcp_project`` (ship-inert), idempotent without ``force`` (a future auto-trigger), and a
no-op for an empty chat. ``custom_analysis_data`` is deferred (always ``None`` this phase).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import ChatAnalysisRecord, ChatMessage
from usan_api.repositories import chat_analyses as chat_analyses_repo
from usan_api.repositories import chats as chats_repo
from usan_api.settings import Settings
from usan_api.vertex_test import run_vertex_turn

_MAX_TRANSCRIPT_CHARS = 12000
_MAX_SUMMARY_CHARS = 4000

# The oracle ChatAnalysis.user_sentiment enum (title-case). Anything else -> None.
_VALID_SENTIMENTS: frozenset[str] = frozenset({"Negative", "Positive", "Neutral", "Unknown"})

_SYSTEM_INSTRUCTION = (
    "You analyze a chat conversation between an assistant agent and a user. "
    "Respond with ONLY a JSON object, no markdown, with keys: "
    '"chat_summary" (a 1-3 sentence high-level recap, warm and factual), '
    '"user_sentiment" (exactly one of: Positive, Negative, Neutral, Unknown), and '
    '"chat_successful" (a boolean: whether the agent seems to have accomplished the '
    "user's goal in the chat). Do not invent details."
)


@dataclass(frozen=True)
class _ParsedAnalysis:
    chat_summary: str | None = None
    user_sentiment: str | None = None
    chat_successful: bool | None = None


def _render_transcript(messages: list[ChatMessage]) -> str:
    lines = [f"{m.role}: {m.content}" for m in messages if m.content and m.content.strip()]
    return "\n".join(lines)[:_MAX_TRANSCRIPT_CHARS]


def _strip_code_fence(text: str) -> str:
    """Drop a leading/trailing ```json fence some models add. Kept local so this pipeline
    has no dependency on the parallel summarization module."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _coerce_sentiment(raw: Any) -> str | None:
    if not isinstance(raw, str):
        return None
    norm = raw.strip().capitalize()  # "POSITIVE"/"positive" -> "Positive"
    return norm if norm in _VALID_SENTIMENTS else None


def _parse_analysis(text: str) -> _ParsedAnalysis:
    """Parse the model's JSON defensively; a non-JSON reply degrades to a raw-text summary."""
    raw_text = (text or "").strip()
    try:
        data = json.loads(_strip_code_fence(raw_text))
    except json.JSONDecodeError, ValueError:
        return _ParsedAnalysis(chat_summary=raw_text[:_MAX_SUMMARY_CHARS] or None)
    if not isinstance(data, dict):
        return _ParsedAnalysis(chat_summary=raw_text[:_MAX_SUMMARY_CHARS] or None)
    summary_raw = data.get("chat_summary")
    summary = (
        str(summary_raw).strip()[:_MAX_SUMMARY_CHARS]
        if isinstance(summary_raw, str) and summary_raw.strip()
        else None
    )
    successful_raw = data.get("chat_successful")
    return _ParsedAnalysis(
        chat_summary=summary,
        user_sentiment=_coerce_sentiment(data.get("user_sentiment")),
        chat_successful=successful_raw if isinstance(successful_raw, bool) else None,
    )


async def analyze_chat_with(
    db: AsyncSession,
    session_id: uuid.UUID,
    settings: Settings,
    *,
    force: bool = False,
) -> ChatAnalysisRecord | None:
    """Analyze one chat and upsert the result. Returns the analysis row, the prior row, or
    None. Flush-only (the caller commits). Never raises — a Vertex/parse failure returns the
    prior record so the inline rerun endpoint still answers 201."""
    existing = await chat_analyses_repo.get_for_session(db, session_id)
    if not (settings.chat_analysis_enabled and settings.gcp_project):
        return existing  # ship-inert / unconfigured -> no PHI leaves, no spend
    if existing is not None and not force:
        return existing  # idempotent: already analyzed (a future auto-trigger path)
    messages = await chats_repo.list_messages(db, session_id)
    if not messages:
        return existing  # nothing to analyze -> no Vertex call
    transcript = _render_transcript(messages)
    try:
        turn = await run_vertex_turn(
            model=settings.chat_analysis_model,
            temperature=0.2,
            system_instruction=_SYSTEM_INSTRUCTION,
            tools=[],
            contents=[{"role": "user", "parts": [{"text": transcript}]}],
            settings=settings,
        )
        parsed = _parse_analysis(turn.text)
        return await chat_analyses_repo.upsert(
            db,
            session_id,
            chat_summary=parsed.chat_summary,
            user_sentiment=parsed.user_sentiment,
            chat_successful=parsed.chat_successful,
            custom_analysis_data=None,
            model_version=settings.chat_analysis_model,
        )
    except Exception as exc:  # noqa: BLE001 - never break the inline endpoint; log TYPE only
        logger.bind(err=type(exc).__name__).error("chat analysis crashed")
        return existing
