"""Tap LiveKit `metrics_collected` events into per-turn latency + per-session usage,
and flush them to the API at call end (design spec §4).

The accumulator dispatches on the metric class *name* and reads fields via getattr,
so it stays decoupled from livekit imports and is unit-testable with fakes. Confirm
the real class/field names against the pinned livekit-agents version (V1).
"""

import time
from typing import Any

from usan_agent import api_client
from usan_agent.settings import Settings


def _ms(seconds: float | None) -> int | None:
    if seconds is None:
        return None
    return max(0, round(float(seconds) * 1000))


class MetricsAccumulator:
    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None
        self.llm_prompt_tokens = 0
        self.llm_completion_tokens = 0
        self.tts_characters = 0
        self.stt_audio_seconds = 0.0

    def _start_turn(self) -> None:
        if self._current is not None:
            self.turns.append(self._current)
        self._current = {"turn_index": len(self.turns)}

    def _ensure_turn(self) -> None:
        if self._current is None:
            self._start_turn()

    def _flush_turn(self) -> None:
        if self._current is not None:
            self.turns.append(self._current)
            self._current = None

    def handle(self, ev: Any) -> None:
        m = ev.metrics
        name = type(m).__name__
        if name == "EOUMetrics":
            self._start_turn()
            assert self._current is not None
            self._current["eou_delay_ms"] = _ms(getattr(m, "end_of_utterance_delay", None))
            self._current["transcription_delay_ms"] = _ms(getattr(m, "transcription_delay", None))
        elif name == "STTMetrics":
            self.stt_audio_seconds += float(getattr(m, "audio_duration", 0.0) or 0.0)
            self._ensure_turn()
            assert self._current is not None
            self._current["stt_duration_ms"] = _ms(getattr(m, "duration", None))
        elif name == "LLMMetrics":
            self.llm_prompt_tokens += int(getattr(m, "prompt_tokens", 0) or 0)
            self.llm_completion_tokens += int(getattr(m, "completion_tokens", 0) or 0)
            self._ensure_turn()
            assert self._current is not None
            self._current["llm_ttft_ms"] = _ms(getattr(m, "ttft", None))
            self._current["llm_completion_tokens"] = getattr(m, "completion_tokens", None)
        elif name == "TTSMetrics":
            self.tts_characters += int(getattr(m, "characters_count", 0) or 0)
            self._ensure_turn()
            assert self._current is not None
            self._current["tts_ttfb_ms"] = _ms(getattr(m, "ttfb", None))
            self._current["tts_characters"] = getattr(m, "characters_count", None)
            self._flush_turn()

    def build_payload(self, *, session_duration_seconds: float | None = None) -> dict[str, Any]:
        self._flush_turn()
        return {
            "turns": self.turns,
            "usage": {
                "llm_prompt_tokens": self.llm_prompt_tokens,
                "llm_completion_tokens": self.llm_completion_tokens,
                "tts_characters": self.tts_characters,
                "stt_audio_seconds": round(self.stt_audio_seconds, 2),
                "session_duration_seconds": session_duration_seconds,
            },
        }


def register_metrics_flush(
    ctx: Any, session: Any, call_id: str, settings: Settings
) -> MetricsAccumulator:
    """Attach a metrics_collected accumulator and a shutdown-callback flush (mirrors
    register_transcript_flush). Returns the accumulator (for tests)."""
    acc = MetricsAccumulator()
    session.on("metrics_collected", lambda ev: acc.handle(ev))
    started = time.monotonic()

    async def _flush() -> None:
        payload = acc.build_payload(session_duration_seconds=round(time.monotonic() - started, 2))
        await api_client.post_metrics(call_id, settings, payload)

    ctx.add_shutdown_callback(_flush)
    return acc
