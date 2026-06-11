"""§5.3 retry policy — ladders as data.

A retry's delay is keyed on the terminal status and the CHAIN-GLOBAL attempt
number that just ended. ``None`` means stop retrying. Pure function — no I/O,
no clock.

Per-profile policy (spec 2026-06-10 §3.3.1) can truncate (``max_attempts=0``),
extend (final-rung repeat past the ladder) or scale (``delay_multiplier``) the
builtin ladders. ``max_attempts`` is chain-global: status can change across a
chain (no_answer → busy → no_answer) and the cap keys on the chain's attempt
number — there are no per-status counters.
"""

from datetime import timedelta

from usan_api.db.base import CallStatus

# Builtin v1 ladders: an attempt N (1-based) ending with <status> waits
# ladder[N - 1] before the next attempt; attempts past the ladder stop unless a
# policy max_attempts extends them (the final rung repeats up to the cap).
_LADDERS: dict[CallStatus, tuple[timedelta, ...]] = {
    CallStatus.NO_ANSWER: (timedelta(minutes=30), timedelta(hours=2)),
    CallStatus.VOICEMAIL_LEFT: (timedelta(hours=3),),
    CallStatus.BUSY: (timedelta(minutes=5),),
    CallStatus.FAILED: (timedelta(minutes=1),),
}

# Single source for the chain-length invariant (spec §3.3.1): root + 4 retries,
# 4 being the RetryMaxAttempts ``le=4`` ceiling in schemas/agent_config.py.
# repositories/calls.py DERIVES ``_MAX_CHAIN_HOPS`` and the ``schedule_retry``
# root-walk bound from this constant; raising ``le=`` without raising this
# reintroduces the chain-tip escape (a depth-4 tip invisible to
# ``get_chain_tip``/``cancel_queued_tips`` and a root walk that never reaches
# the ``batch:`` root).
MAX_CHAIN_ATTEMPTS = 5


def next_retry_delay(
    status: CallStatus,
    attempt: int,
    *,
    max_attempts: int | None = None,
    delay_multiplier: float = 1.0,
) -> timedelta | None:
    """Delay before the next attempt, or None when the policy says stop.

    ``attempt`` is the chain-global attempt number that just ended (1-based).
    ``max_attempts`` replaces the ladder length as the retry cap (chain-global
    semantics, see module docstring); ``delay_multiplier`` scales every rung.
    Defaults reproduce the builtin v1 ladder exactly.
    """
    ladder = _LADDERS.get(status)
    if ladder is None or attempt < 1:
        return None
    limit = max_attempts if max_attempts is not None else len(ladder)
    if attempt > limit:
        return None
    return ladder[min(attempt, len(ladder)) - 1] * delay_multiplier
