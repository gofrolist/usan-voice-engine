"""wellbeing_survey_results repository (US6 / T060).

The monthly wellbeing survey, anchored to a first-of-month DATE in the elder's local
timezone. ``upsert_for_month`` is once-per-month idempotent via the unique
``(elder_id, period_month)`` (ON CONFLICT DO NOTHING then return-existing), so a repeated
``record_survey`` the same month writes nothing and returns the original row (FR-032 /
SC-008). ``exists_for_month`` backs the ``survey_due`` builtin. All functions are
flush-only; the caller commits.
"""

import uuid
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import WellbeingSurveyResult


def month_anchor(timezone: str, now: datetime) -> date:
    """First-of-month anchor for ``now`` in the elder's local month (FR-032).

    Falls back to ``now``'s UTC date on an empty/garbled timezone, exactly like
    ``builtin_vars._elder_today`` — a bad tz never crashes survey recording, it just
    anchors to the server-day month.
    """
    local = now.date()
    if timezone:
        try:
            local = now.astimezone(ZoneInfo(timezone)).date()
        except ZoneInfoNotFoundError, ValueError, KeyError:
            local = now.date()
    return local.replace(day=1)


async def _get(db: AsyncSession, survey_id: int) -> WellbeingSurveyResult | None:
    return (
        await db.execute(select(WellbeingSurveyResult).where(WellbeingSurveyResult.id == survey_id))
    ).scalar_one_or_none()


async def get_for_month(
    db: AsyncSession, *, elder_id: uuid.UUID, period_month: date
) -> WellbeingSurveyResult | None:
    """The elder's survey row for ``period_month``, if one exists."""
    return (
        await db.execute(
            select(WellbeingSurveyResult).where(
                WellbeingSurveyResult.elder_id == elder_id,
                WellbeingSurveyResult.period_month == period_month,
            )
        )
    ).scalar_one_or_none()


async def exists_for_month(db: AsyncSession, *, elder_id: uuid.UUID, period_month: date) -> bool:
    """Whether the elder already has a survey this month (``survey_due`` is its negation)."""
    found = (
        await db.execute(
            select(WellbeingSurveyResult.id).where(
                WellbeingSurveyResult.elder_id == elder_id,
                WellbeingSurveyResult.period_month == period_month,
            )
        )
    ).scalar_one_or_none()
    return found is not None


async def upsert_for_month(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    elder_id: uuid.UUID,
    period_month: date,
    loneliness: int | None,
    mood: int | None,
    satisfaction: int | None,
    raw: dict[str, Any] | None = None,
) -> tuple[WellbeingSurveyResult, bool]:
    """Record this month's survey once. Returns ``(row, created)``.

    INSERT ... ON CONFLICT (elder_id, period_month) DO NOTHING: a first call inserts and
    returns ``(row, True)``; a repeat the same month inserts nothing and returns the
    existing row as ``(row, False)`` (FR-032). Flush-only.
    """
    insert_stmt = (
        pg_insert(WellbeingSurveyResult)
        .values(
            call_id=call_id,
            elder_id=elder_id,
            period_month=period_month,
            loneliness=loneliness,
            mood=mood,
            satisfaction=satisfaction,
            raw=raw or {},
        )
        .on_conflict_do_nothing(
            index_elements=[
                WellbeingSurveyResult.elder_id,
                WellbeingSurveyResult.period_month,
            ]
        )
        .returning(WellbeingSurveyResult.id)
    )
    new_id = (await db.execute(insert_stmt)).scalar_one_or_none()
    if new_id is not None:
        await db.flush()
        created = await _get(db, new_id)
        assert created is not None  # just inserted in this txn
        return created, True
    existing = await get_for_month(db, elder_id=elder_id, period_month=period_month)
    if existing is None:  # pragma: no cover - the conflict guarantees a row exists
        raise RuntimeError("survey conflict without an existing row")
    return existing, False
