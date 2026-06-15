"""T010 (US1): deterministic crisis safety-net matcher.

Mirrors test_voicemail.py. The matcher is the deterministic layer that escalates
even when the LLM misses a crisis (FR-002). It must be HIGH-PRECISION: a benign
control set keeps the false-positive rate <= 2% (SC-002).
"""

import pytest

from usan_agent.crisis_watcher import CrisisWatcher, detect_crisis

# Explicit crisis utterances per category -> the category they must classify as.
_POSITIVE: list[tuple[str, str]] = [
    ("I want to kill myself", "suicidal"),
    ("I don't want to live anymore", "suicidal"),
    ("I've been having thoughts of suicide", "suicidal"),
    ("I think I took too many pills", "overdose"),
    ("I may have overdosed on my medicine", "overdose"),
    ("I have terrible chest pain right now", "medical"),
    ("I can't breathe", "medical"),
    ("I think I'm having a heart attack", "medical"),
    ("someone is hurting me", "abuse"),
    ("my son keeps hitting me", "abuse"),
    ("I don't know where I am", "confusion"),
    ("I'm so confused, I can't remember who you are", "confusion"),
]

# Benign control set — none of these is a crisis (SC-002 false-positive ceiling).
_BENIGN: list[str] = [
    "Hello, how are you today?",
    "I had chicken and rice for lunch.",
    "My favorite color is blue.",
    "The weather is lovely this morning.",
    "My grandson came to visit yesterday.",
    "I watched my favorite show last night.",
    "I took my medication this morning, thank you.",
    "My knee aches a little when it rains.",
    "I'm feeling pretty good today.",
    "I went for a short walk in the garden.",
    "Can you remind me about my doctor's appointment?",
    "I read a wonderful book about birds.",
    "The grandchildren are growing so fast.",
    "I had a good night's sleep.",
    "I'm a bit tired but otherwise fine.",
    "My daughter is coming over for dinner.",
    "I enjoy listening to the radio in the afternoon.",
    "The flowers in the yard are blooming.",
    "I made some tea earlier.",
    "Thank you for calling to check on me.",
]


@pytest.mark.parametrize(("text", "category"), _POSITIVE)
def test_detect_crisis_positive(text, category):
    assert detect_crisis(text) == category


def test_detect_crisis_false_positive_rate_within_budget():
    # SC-002: benign control set must stay at or under a 2% false-positive rate.
    false_positives = [t for t in _BENIGN if detect_crisis(t) is not None]
    rate = len(false_positives) / len(_BENIGN)
    assert rate <= 0.02, f"false positives: {false_positives}"


def test_watcher_accumulates_across_chunks():
    w = CrisisWatcher()
    assert w.feed("I have terrible") is None
    assert w.detected_category is None
    assert w.feed("chest pain") == "medical"  # phrase completes across chunks
    assert w.detected_category == "medical"


def test_watcher_only_fires_once():
    w = CrisisWatcher()
    assert w.feed("I want to kill myself") == "suicidal"
    # Already detected -> a later crisis phrase does not re-fire.
    assert w.feed("I can't breathe") is None
    assert w.detected_category == "suicidal"


def test_watcher_ignores_benign_speech():
    w = CrisisWatcher()
    for phrase in _BENIGN:
        assert w.feed(phrase) is None
    assert w.detected_category is None


@pytest.mark.asyncio
async def test_watcher_feed_is_sync_and_usable_from_event_handler():
    # The worker calls feed() from a sync on("user_input_transcribed") handler, then
    # spawns the async escalation. Confirm feed() needs no await and returns promptly.
    w = CrisisWatcher()

    async def _drive():
        return w.feed("I think I took too many pills")

    assert await _drive() == "overdose"
