from types import SimpleNamespace

from usan_agent.metrics_hooks import MetricsAccumulator


class EOUMetrics:
    def __init__(self, end_of_utterance_delay, transcription_delay):
        self.end_of_utterance_delay = end_of_utterance_delay
        self.transcription_delay = transcription_delay


class STTMetrics:
    def __init__(self, audio_duration, duration):
        self.audio_duration = audio_duration
        self.duration = duration


class LLMMetrics:
    def __init__(self, ttft, prompt_tokens, completion_tokens):
        self.ttft = ttft
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class TTSMetrics:
    def __init__(self, ttfb, characters_count):
        self.ttfb = ttfb
        self.characters_count = characters_count


def _ev(metric):
    return SimpleNamespace(metrics=metric)


def test_single_full_turn():
    acc = MetricsAccumulator()
    acc.handle(_ev(EOUMetrics(0.18, 0.12)))
    acc.handle(_ev(STTMetrics(3.0, 0.09)))
    acc.handle(_ev(LLMMetrics(0.21, 100, 50)))
    acc.handle(_ev(TTSMetrics(0.08, 240)))

    payload = acc.build_payload(session_duration_seconds=42.0)
    assert payload["turns"] == [
        {
            "turn_index": 0,
            "eou_delay_ms": 180,
            "transcription_delay_ms": 120,
            "stt_duration_ms": 90,
            "llm_ttft_ms": 210,
            "llm_completion_tokens": 50,
            "tts_ttfb_ms": 80,
            "tts_characters": 240,
        }
    ]
    assert payload["usage"] == {
        "llm_prompt_tokens": 100,
        "llm_completion_tokens": 50,
        "tts_characters": 240,
        "stt_audio_seconds": 3.0,
        "session_duration_seconds": 42.0,
    }


def test_two_turns_increment_index():
    acc = MetricsAccumulator()
    for _ in range(2):
        acc.handle(_ev(EOUMetrics(0.1, 0.1)))
        acc.handle(_ev(LLMMetrics(0.2, 10, 5)))
        acc.handle(_ev(TTSMetrics(0.05, 100)))
    payload = acc.build_payload(session_duration_seconds=10.0)
    assert [t["turn_index"] for t in payload["turns"]] == [0, 1]
    assert payload["usage"]["llm_prompt_tokens"] == 20
    assert payload["usage"]["tts_characters"] == 200


def test_trailing_incomplete_turn_is_flushed():
    acc = MetricsAccumulator()
    acc.handle(_ev(EOUMetrics(0.1, 0.1)))
    acc.handle(_ev(LLMMetrics(0.2, 10, 5)))  # no TTS this turn
    payload = acc.build_payload(session_duration_seconds=5.0)
    assert len(payload["turns"]) == 1
    assert payload["turns"][0]["llm_ttft_ms"] == 200
    assert "tts_ttfb_ms" not in payload["turns"][0]
