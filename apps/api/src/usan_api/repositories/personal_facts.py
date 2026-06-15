"""personal_facts repository (US4 / T046).

Durable categorized memory about an elder. ``create`` is used by the
``record_personal_fact`` tool (``source='elder_stated'``) and the post-call summarizer
(``source='extracted'``). ``list_active`` is the source of the ``personal_facts`` /
``important_dates`` built-ins. All functions are flush-only; the caller commits.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import PersonalFact

# Bound the per-elder injection so a long memory never floods the next prompt. The
# build_vars sanitizer caps each value at 300 chars on top of this row cap.
_MAX_ACTIVE_INJECT = 50


async def create(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID,
    category: str,
    content: str,
    structured: dict[str, Any] | None = None,
    source: str = "elder_stated",
    phi: bool | None = None,
) -> PersonalFact:
    """Insert one personal fact. Omitted ``structured``/``phi`` take their DB defaults
    (``{}`` / ``true``) — ``phi`` is true unless the caller proves otherwise."""
    row = PersonalFact(elder_id=elder_id, category=category, content=content, source=source)
    if structured is not None:
        row.structured = structured
    if phi is not None:
        row.phi = phi
    db.add(row)
    await db.flush()
    return row


async def list_active(
    db: AsyncSession, *, elder_id: uuid.UUID, limit: int = _MAX_ACTIVE_INJECT
) -> list[PersonalFact]:
    """Active facts for an elder (oldest first) — the source of the memory built-ins."""
    stmt = (
        select(PersonalFact)
        .where(PersonalFact.elder_id == elder_id, PersonalFact.active.is_(True))
        .order_by(PersonalFact.created_at, PersonalFact.id)
        .limit(max(1, limit))
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_active_keys(db: AsyncSession, *, elder_id: uuid.UUID) -> set[tuple[str, str]]:
    """Every active (category, content) key for an elder — UNCAPPED, for extraction dedup.

    Distinct from ``list_active`` (which caps the per-call prompt injection at 50): the
    summarizer must dedup extracted facts against the FULL active set, or a duplicate fact
    sitting beyond the 50-row injection window would be re-inserted on every call (slow
    unbounded growth). Selects only the two columns the dedup compares.
    """
    stmt = select(PersonalFact.category, PersonalFact.content).where(
        PersonalFact.elder_id == elder_id, PersonalFact.active.is_(True)
    )
    return {(category, content) for category, content in (await db.execute(stmt)).all()}
