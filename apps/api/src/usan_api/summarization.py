"""Post-call summarization + fact extraction (US4 / T049-T050; design §memory).

After a call completes, one Vertex turn over the transcript produces a short recap,
the elder's open follow-up plans, and any durable facts worth remembering. The recap +
plans become the ``last_call_summary`` / ``open_plans`` built-ins and the facts become
``personal_facts`` (``source='extracted'``) — all carried into the elder's next call.

PHI containment (Constitution II): the transcript is PHI. It is sent ONLY to Vertex AI
via ``vertexai=True`` + ADC (reusing ``vertex_test.run_vertex_turn``) — NEVER the Gemini
Developer API — and the recap/facts land only in our BAA Postgres. Nothing here logs
transcript or summary text; only ``call_id`` + counts.

``summarize_call_with`` is the testable core (idempotent per call, a no-op when there is
nothing to summarize). ``summarize_call`` is the fire-and-forget background wrapper: it
is flag-gated (ship-inert by default) and opens its own session, like ``flush_pending_sms``.
"""

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, get_args

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import ConversationSummary
from usan_api.db.session import get_session_factory
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import conversation_summaries as summaries_repo
from usan_api.repositories import personal_facts as personal_facts_repo
from usan_api.repositories import transcripts as transcripts_repo
from usan_api.schemas.personalization import FactCategory
from usan_api.settings import Settings, get_settings
from usan_api.vertex_test import run_vertex_turn

# Mirror of the personal_facts.category CHECK — extracted facts off this enum are dropped
# (a hallucinated category must never reach the DB).
_VALID_CATEGORIES: frozenset[str] = frozenset(get_args(FactCategory))

# Defensive bounds: cap the transcript fed to the model and the stored recap length.
_MAX_TRANSCRIPT_CHARS = 12000
_MAX_SUMMARY_CHARS = 4000
_MAX_PLAN_CHARS = 300
_MAX_FACT_CONTENT_CHARS = 500
_MAX_EXTRACTED_FACTS = 20

_SYSTEM_INSTRUCTION = (
    "You summarize a wellness check-in call with an elderly person for the next call's "
    "context. Respond with ONLY a JSON object, no markdown, with keys: "
    '"summary" (1-3 sentence recap, warm and factual), '
    '"open_plans" (array of short strings: things the elder said they intend to do or '
    "want followed up, e.g. 'see the doctor Tuesday'; empty array if none), and "
    '"facts" (array of durable facts worth remembering long-term, each an object with '
    '"category" (one of: person, routine, preference, important_date, health_context), '
    '"content" (a short natural-language fact), and optional "structured" (an object, '
    'e.g. {"date":"2026-07-04","label":"birthday"} for an important_date). '
    "Only include facts that are durable and clearly stated; use an empty array if unsure. "
    "Do not invent details."
)


@dataclass(frozen=True)
class _ParsedFact:
    category: str
    content: str
    structured: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _ParsedSummary:
    summary: str
    open_plans: list[str] = field(default_factory=list)
    facts: list[_ParsedFact] = field(default_factory=list)


def _render_transcript(segments: list[Any]) -> str:
    lines = []
    for seg in segments:
        role = getattr(seg, "role", None) or "unknown"
        content = getattr(seg, "content", None)
        if isinstance(content, str) and content.strip():
            lines.append(f"{role}: {content.strip()}")
    return "\n".join(lines)[:_MAX_TRANSCRIPT_CHARS]


def _strip_code_fence(text: str) -> str:
    """Drop a leading/trailing ```json fence some models add despite the instruction."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t[3:]
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _coerce_facts(raw: Any) -> list[_ParsedFact]:
    facts: list[_ParsedFact] = []
    if not isinstance(raw, list):
        return facts
    for item in raw:
        if not isinstance(item, dict):
            continue
        category = item.get("category")
        content = item.get("content")
        if category not in _VALID_CATEGORIES:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        structured = item.get("structured")
        facts.append(
            _ParsedFact(
                category=category,
                content=content.strip()[:_MAX_FACT_CONTENT_CHARS],
                structured=structured if isinstance(structured, dict) else {},
            )
        )
        if len(facts) >= _MAX_EXTRACTED_FACTS:
            break
    return facts


def _parse_summary(text: str) -> _ParsedSummary:
    """Parse the model's JSON defensively; a non-JSON reply degrades to a raw-text recap."""
    raw_text = (text or "").strip()
    try:
        data = json.loads(_strip_code_fence(raw_text))
    except json.JSONDecodeError, ValueError:
        # The model ignored the JSON instruction — keep its prose as the recap rather
        # than losing the turn (and never raise; Observability VI).
        return _ParsedSummary(summary=raw_text[:_MAX_SUMMARY_CHARS])
    if not isinstance(data, dict):
        return _ParsedSummary(summary=raw_text[:_MAX_SUMMARY_CHARS])
    summary = str(data.get("summary") or "").strip()[:_MAX_SUMMARY_CHARS]
    plans_raw = data.get("open_plans")
    open_plans = [
        p.strip()[:_MAX_PLAN_CHARS]
        for p in (plans_raw if isinstance(plans_raw, list) else [])
        if isinstance(p, str) and p.strip()
    ]
    return _ParsedSummary(
        summary=summary, open_plans=open_plans, facts=_coerce_facts(data.get("facts"))
    )


async def summarize_call_with(
    db: AsyncSession, call_id: uuid.UUID, settings: Settings
) -> ConversationSummary | None:
    """Summarize one call and persist the recap + extracted facts. Returns the summary
    row, or None when there is nothing to do (already summarized / no transcript)."""
    if await summaries_repo.get_for_call(db, call_id) is not None:
        return None  # idempotent: a prior trigger already summarized this call
    call = await calls_repo.get_call(db, call_id)
    if call is None or call.elder_id is None:
        return None
    segments = await transcripts_repo.list_for_call(db, call_id)
    transcript_text = _render_transcript(segments)
    if not transcript_text:
        return None  # no transcript yet -> nothing to summarize, no Vertex call

    turn = await run_vertex_turn(
        model=settings.summarization_model,
        temperature=0.2,
        system_instruction=_SYSTEM_INSTRUCTION,
        tools=[],
        contents=[{"role": "user", "parts": [{"text": transcript_text}]}],
        settings=settings,
    )
    parsed = _parse_summary(turn.text)

    summary_row = await summaries_repo.create(
        db,
        call_id=call_id,
        elder_id=call.elder_id,
        summary=parsed.summary,
        open_plans=parsed.open_plans,
        model_version=settings.summarization_model,
    )
    if summary_row is None:
        # Lost a race to a concurrent trigger — it owns the facts too; don't double-write.
        return None

    # Extracted facts, skipping ones already active for this elder (avoid re-adding the
    # same fact every call). source='extracted' — never forge an elder_stated fact here.
    # Dedup against the FULL active key set (uncapped) — list_active's 50-row injection cap
    # would let a duplicate beyond row 50 be re-inserted every call (unbounded growth).
    existing = await personal_facts_repo.list_active_keys(db, elder_id=call.elder_id)
    for fact in parsed.facts:
        if (fact.category, fact.content) in existing:
            continue
        existing.add((fact.category, fact.content))
        await personal_facts_repo.create(
            db,
            elder_id=call.elder_id,
            category=fact.category,
            content=fact.content,
            structured=fact.structured or None,
            source="extracted",
        )
    await db.commit()
    logger.bind(call_id=str(call_id), facts=len(parsed.facts)).info("Summarized call")
    return summary_row


async def summarize_call(call_id: uuid.UUID) -> None:
    """Fire-and-forget background trigger (end_call / room_finished). Flag-gated and
    self-contained: opens its own session, swallows + logs failures (never crashes the
    request), and makes NO Vertex call when summarization is disabled or unconfigured."""
    settings = get_settings()
    if not (settings.summarization_enabled and settings.gcp_project):
        return  # ship-inert by default / unconfigured -> no PHI leaves, no spend
    try:
        factory = get_session_factory()
        async with factory() as db:
            await summarize_call_with(db, call_id, settings)
    except Exception as exc:  # noqa: BLE001 - fire-and-forget; log TYPE only (PHI-safe)
        logger.bind(call_id=str(call_id), err=type(exc).__name__).error("summarization crashed")
