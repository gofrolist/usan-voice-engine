"""Transcript-based voicemail detection (design spec §7).

Telnyx AMD is not invokable in our LiveKit-SIP-trunk topology, so voicemail is
detected agent-side from the first few seconds of STT. `is_voicemail` is a pure
classifier; `VoicemailWatcher` accumulates interim+final STT chunks and signals
once a voicemail greeting is recognised within the detection window.
"""

import asyncio
import re

# Seconds after the callee answers to listen for a voicemail greeting.
VOICEMAIL_WINDOW_S = 3.0

# §7 patterns, case-insensitive. The apostrophe in "you've" may be transcribed
# as a straight ', a curly ’, or dropped, so the class covers both and is
# optional. (re interprets the ’ escape inside the pattern.)
_PATTERN = re.compile(
    r"leave a (?:message|name)"
    r"|you[’']?ve reached"
    r"|not available right now"
    r"|after the (?:beep|tone)",
    re.IGNORECASE,
)


def is_voicemail(text: str) -> bool:
    return bool(_PATTERN.search(text))


def build_matcher(trigger_phrases: list[str]) -> "re.Pattern[str]":
    """Compile a case-insensitive, LITERAL matcher from admin trigger phrases.

    Empty (or all-blank) phrases -> the built-in §7 _PATTERN. Phrases are re.escape'd
    and OR-joined so admin input is matched literally (never as a regex) — a false
    positive would hang up on a live elder.
    """
    cleaned = [p for p in trigger_phrases if p and p.strip()]
    if not cleaned:
        return _PATTERN
    return re.compile("|".join(re.escape(p) for p in cleaned), re.IGNORECASE)


class VoicemailWatcher:
    """Accumulate STT chunks and flag when a voicemail greeting is recognised."""

    def __init__(self, matcher: "re.Pattern[str] | None" = None) -> None:
        self._buffer = ""
        self._event = asyncio.Event()
        self._matcher = matcher or _PATTERN

    def feed(self, transcript: str) -> None:
        if self._event.is_set():
            return  # already detected; stop accumulating
        # Interim chunks are revised rather than strictly additive, but matching
        # the §7 phrases against the running buffer is robust to that.
        self._buffer = f"{self._buffer} {transcript}".strip()
        if self._matcher.search(self._buffer):
            self._event.set()

    @property
    def detected(self) -> bool:
        return self._event.is_set()

    async def wait_until_detected(self, window_s: float) -> bool:
        """True if a voicemail greeting is detected within `window_s` seconds."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=window_s)
            return True
        except TimeoutError:
            return False
