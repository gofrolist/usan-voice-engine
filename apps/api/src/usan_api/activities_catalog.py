"""The mood-boosting activity catalog + least-recently-used selection (US6 / T061).

The catalog itself is CODE (operator-curatable in a later iteration, like
``emergency_resources``): each entry is a small, self-contained breathing exercise,
memory exercise, or light game with a spoken ``script`` the agent reads warmly. Per-elder
*recent use* lives in the ``activity_history`` table, not here.

``select_activity`` is the pure non-repeat policy (FR-034 / SC-009): it never repeats an
activity that was used within the last 30 days OR is among the elder's last 3 used (the
union — the larger exclusion set), preferring a never-used one for variety, then the
least-recently-used. When every candidate is excluded (the catalog is exhausted for this
elder) it falls back to the least-recently-used overall. Pure + deterministic so the
``get_activity`` endpoint stays a thin orchestrator (load history -> select -> record use).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol

# Concrete activity kinds (the catalog). ``ActivityKindFilter`` adds the request-only
# "any" sentinel used by get_activity to mean "any kind".
ActivityKind = Literal["breathing", "memory", "game"]
ActivityKindFilter = Literal["any", "breathing", "memory", "game"]


@dataclass(frozen=True)
class Activity:
    """One catalog activity. ``script`` is spoken to the elder; keep it calm and concrete."""

    key: str
    kind: ActivityKind
    title: str
    script: str


# Recency policy (FR-034). "Recently used" = used within the last 30 days OR among the
# elder's last 3 uses — the union (whichever set is larger). Tunable in one place.
RECENT_DAYS = 30
RECENT_COUNT = 3


# The catalog, in display order. Two entries per kind so the non-repeat policy has room
# to vary within a single kind before exhausting it.
ACTIVITIES: tuple[Activity, ...] = (
    Activity(
        key="box_breathing",
        kind="breathing",
        title="Box breathing",
        script=(
            "Let's take a slow breath together. Breathe in gently through your nose while I "
            "count to four... one, two, three, four. Hold for four... and now breathe out "
            "slowly for four. Let's do that a few times, nice and easy."
        ),
    ),
    Activity(
        key="four_seven_eight_breathing",
        kind="breathing",
        title="4-7-8 breathing",
        script=(
            "Here's a calming breath. Breathe in quietly through your nose for four counts... "
            "hold it softly for seven... and let it out slowly through your mouth for eight. "
            "We'll do it together a couple of times — there's no rush at all."
        ),
    ),
    Activity(
        key="three_good_things",
        kind="memory",
        title="Three good things",
        script=(
            "Let's think of three good things from today, however small — maybe a nice cup of "
            "tea, a bit of sunshine, or a friendly voice. Take your time, and tell me the first "
            "one that comes to mind."
        ),
    ),
    Activity(
        key="favorite_place",
        kind="memory",
        title="A favorite place",
        script=(
            "Let's picture a place you've always loved — a garden, a kitchen, a seaside. "
            "Tell me what you see there, and what sounds or smells you remember. I'd love to "
            "hear about it."
        ),
    ),
    Activity(
        key="word_association",
        kind="game",
        title="Word association",
        script=(
            "Let's play a gentle word game. I'll say a word, and you tell me the very first "
            "word it brings to mind — there are no wrong answers. Ready? Let's start with: "
            "sunshine."
        ),
    ),
    Activity(
        key="i_spy",
        kind="game",
        title="I spy",
        script=(
            "Let's play a little 'I spy.' Look around the room and find something with a "
            "color you like. Don't tell me what it is yet — just describe its color and shape, "
            "and I'll try to guess."
        ),
    ),
)

_BY_KEY: dict[str, Activity] = {a.key: a for a in ACTIVITIES}


class _Use(Protocol):
    """Structural view of an ``activity_history`` row (or any most-recent-first record)."""

    activity_key: str
    used_at: datetime


def by_key(key: str) -> Activity:
    """The activity for ``key`` (KeyError if unknown — keys are server-controlled)."""
    return _BY_KEY[key]


def list_activities(kind: ActivityKindFilter | None) -> list[Activity]:
    """Catalog entries of ``kind`` ("any"/None -> all), preserving catalog order."""
    if kind in (None, "any"):
        return list(ACTIVITIES)
    return [a for a in ACTIVITIES if a.kind == kind]


def select_activity(
    kind: ActivityKindFilter, history: Sequence[_Use], *, now: datetime
) -> Activity:
    """Pick a mood-boosting activity not used recently (FR-034 / SC-009).

    ``history`` is the elder's ``activity_history`` rows, MOST-RECENT-FIRST. Excludes
    anything used within ``RECENT_DAYS`` or among the last ``RECENT_COUNT`` uses (the
    union), then returns the never-used or least-recently-used candidate. When all
    candidates are excluded (catalog exhausted), returns the least-recently-used overall.
    Deterministic: ties break by catalog order.
    """
    candidates = list_activities(kind)
    kind_keys = {a.key for a in candidates}
    relevant = [u for u in history if u.activity_key in kind_keys]

    cutoff = now - timedelta(days=RECENT_DAYS)
    within_window = {u.activity_key for u in relevant if u.used_at >= cutoff}
    last_n = {u.activity_key for u in relevant[:RECENT_COUNT]}
    excluded = within_window | last_n

    # Most-recent use per key (history is desc, so the first hit is the latest).
    last_used: dict[str, datetime] = {}
    for u in relevant:
        last_used.setdefault(u.activity_key, u.used_at)

    pool = [a for a in candidates if a.key not in excluded] or candidates
    # Never-used (no history) sorts before any real timestamp; then least-recently-used.
    # ``min`` returns the first minimal element, so catalog order breaks ties.
    never_used = datetime.min.replace(tzinfo=now.tzinfo)
    return min(pool, key=lambda a: last_used.get(a.key, never_used))
