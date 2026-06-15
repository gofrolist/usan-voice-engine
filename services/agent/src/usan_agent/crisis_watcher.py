"""Deterministic crisis safety-net matcher (US1 / FR-002).

Mirrors voicemail.VoicemailWatcher: accumulates interim+final STT chunks and, when an
explicit crisis phrase is recognized, reports the category. This is the DETERMINISTIC
layer that escalates even when the LLM misses the crisis — the life-safety guarantee.
The agent worker subscribes a CrisisWatcher to ``user_input_transcribed`` and, on a
match, calls ``api_client.raise_crisis`` (detection_source="safety_net") and speaks the
returned resource script.

Patterns are HIGH-PRECISION explicit statements, not broad keywords, so a benign
mention ("my knee aches") never triggers — the false-positive rate stays <= 2% (SC-002,
covered by the benign control set in test_crisis_watcher.py).
"""

import re

from usan_agent.api_client import CrisisCategory

# Apostrophe class: STT may emit a straight ', a curly ’, or drop it entirely.
_APOS = "[’']?"

# Per-category patterns, evaluated IN ORDER (first match wins). The five categories
# mirror the API's CrisisCategory: suicidal | overdose | medical | abuse | confusion.
# Typed as CrisisCategory so the detected value is enum-checked end to end (the worker
# hands it straight to api_client.raise_crisis, which requires the literal).
_CATEGORY_PATTERNS: tuple[tuple[CrisisCategory, "re.Pattern[str]"], ...] = (
    (
        "suicidal",
        re.compile(
            r"kill(?:ing)? myself"
            r"|end(?:ing)? my life"
            r"|take my own life"
            r"|want to die"
            rf"|do{_APOS}n{_APOS}?t want to live"
            r"|thoughts? of suicide"
            r"|suicid",
            re.IGNORECASE,
        ),
    ),
    (
        "overdose",
        re.compile(
            r"overdos|too many pills|took too many",
            re.IGNORECASE,
        ),
    ),
    (
        "medical",
        re.compile(
            r"chest pain"
            rf"|can{_APOS}?(?:no)?t breathe"
            r"|cannot breathe"
            r"|heart attack"
            r"|having a stroke"
            rf"|fell and can{_APOS}?t get up",
            re.IGNORECASE,
        ),
    ),
    (
        "abuse",
        re.compile(
            r"hurting me"
            r"|hitting me"
            r"|someone is hurting"
            r"|being abused"
            r"|abusing me"
            r"|threaten(?:ed|ing) me",
            re.IGNORECASE,
        ),
    ),
    (
        "confusion",
        re.compile(
            rf"do{_APOS}n{_APOS}?t know where i am"
            r"|where am i"
            r"|who am i"
            r"|so confused"
            rf"|can{_APOS}?t remember who",
            re.IGNORECASE,
        ),
    ),
)


def detect_crisis(text: str) -> CrisisCategory | None:
    """The crisis category an utterance matches, or None. First category wins."""
    for category, pattern in _CATEGORY_PATTERNS:
        if pattern.search(text):
            return category
    return None


class CrisisWatcher:
    """Accumulate STT chunks and report the crisis category once one is recognized.

    Fires AT MOST ONCE per call (the first detected category): the worker escalates on
    that single signal and speaks the resource; a later phrase must not re-escalate.
    ``feed`` is synchronous so it can run inside the sync ``user_input_transcribed``
    event handler; the worker spawns the async escalation when it returns a category.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._category: CrisisCategory | None = None

    def feed(self, transcript: str) -> CrisisCategory | None:
        """Add a chunk; return the crisis category on the FIRST detection, else None."""
        if self._category is not None:
            return None  # already detected; stop escalating
        # Interim chunks are revised rather than strictly additive, but matching the
        # explicit phrases against the running buffer is robust to that (like voicemail).
        self._buffer = f"{self._buffer} {transcript}".strip()
        category = detect_crisis(self._buffer)
        if category is not None:
            self._category = category
            return category
        return None

    @property
    def detected_category(self) -> CrisisCategory | None:
        return self._category
