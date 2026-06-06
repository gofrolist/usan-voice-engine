import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import CallMetrics, TurnMetrics


def response_latency_ms(
    transcription_delay_ms: int | None,
    llm_ttft_ms: int | None,
    tts_ttfb_ms: int | None,
) -> int | None:
    """User-perceived end-of-turn responsiveness (design spec §7). NULL if no parts."""
    parts = [p for p in (transcription_delay_ms, llm_ttft_ms, tts_ttfb_ms) if p is not None]
    return sum(parts) if parts else None


async def get_call_metrics(db: AsyncSession, call_id: uuid.UUID) -> CallMetrics | None:
    return await db.get(CallMetrics, call_id)


async def create_metrics(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    turns: list[Any],
    usage: Any,
    costs: dict[str, Decimal],
    duration_seconds: int | None,
    pricing_version: str,
) -> CallMetrics:
    """Insert per-turn rows and one call_metrics row. Handler owns the commit."""
    for t in turns:
        db.add(
            TurnMetrics(
                call_id=call_id,
                turn_index=t.turn_index,
                eou_delay_ms=t.eou_delay_ms,
                transcription_delay_ms=t.transcription_delay_ms,
                stt_duration_ms=t.stt_duration_ms,
                llm_ttft_ms=t.llm_ttft_ms,
                tts_ttfb_ms=t.tts_ttfb_ms,
                llm_completion_tokens=t.llm_completion_tokens,
                tts_characters=t.tts_characters,
                response_latency_ms=response_latency_ms(
                    t.transcription_delay_ms, t.llm_ttft_ms, t.tts_ttfb_ms
                ),
            )
        )
    row = CallMetrics(
        call_id=call_id,
        llm_prompt_tokens=usage.llm_prompt_tokens,
        llm_completion_tokens=usage.llm_completion_tokens,
        llm_total_tokens=usage.llm_prompt_tokens + usage.llm_completion_tokens,
        tts_characters=usage.tts_characters,
        stt_audio_seconds=Decimal(str(usage.stt_audio_seconds)),
        duration_seconds=duration_seconds,
        cost_telephony_usd=costs["telephony"],
        cost_llm_usd=costs["llm"],
        cost_stt_usd=costs["stt"],
        cost_tts_usd=costs["tts"],
        cost_storage_usd=costs["storage"],
        cost_total_usd=costs["total"],
        pricing_version=pricing_version,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row
