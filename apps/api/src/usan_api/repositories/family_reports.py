"""family_reports repository (US8 / T078).

One row per elder per calendar month. ``create`` is once-per-month idempotent via the
unique ``(elder_id, period_month)`` (ON CONFLICT DO NOTHING then None), so the monthly job
generating the same period twice writes nothing (FR-012 / SC-012). All functions are
flush-only unless noted; the caller commits.
"""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import Elder, FamilyReport

MAX_REPORTS_LIMIT = 500


async def get_for_month(
    db: AsyncSession, *, elder_id: uuid.UUID, period_month: date
) -> FamilyReport | None:
    """The elder's report for ``period_month``, if one exists (idempotency pre-check)."""
    return (
        await db.execute(
            select(FamilyReport).where(
                FamilyReport.elder_id == elder_id,
                FamilyReport.period_month == period_month,
            )
        )
    ).scalar_one_or_none()


async def create(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID,
    period_month: date,
    calls_completed: int,
    metrics: dict[str, Any],
    narrative: str,
    model_version: str,
    status: str,
) -> FamilyReport | None:
    """Insert the month's report once. Returns the row, or None on a same-month conflict.

    INSERT ... ON CONFLICT (elder_id, period_month) DO NOTHING: the first writer inserts and
    gets the row; a concurrent second writer gets None and skips (so it never double-sends
    the family SMS). Flush-only.
    """
    insert_stmt = (
        pg_insert(FamilyReport)
        .values(
            elder_id=elder_id,
            period_month=period_month,
            calls_completed=calls_completed,
            metrics=metrics,
            narrative=narrative,
            model_version=model_version,
            status=status,
        )
        .on_conflict_do_nothing(index_elements=[FamilyReport.elder_id, FamilyReport.period_month])
        .returning(FamilyReport.id)
    )
    new_id = (await db.execute(insert_stmt)).scalar_one_or_none()
    if new_id is None:
        return None  # another worker already generated this elder's report for the month
    await db.flush()
    return (await db.execute(select(FamilyReport).where(FamilyReport.id == new_id))).scalar_one()


async def get_report(db: AsyncSession, report_id: int) -> FamilyReport | None:
    return (
        await db.execute(
            select(FamilyReport)
            .where(FamilyReport.id == report_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def list_reports(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[tuple[FamilyReport, str | None]]:
    """Reports + elder name via outerjoin (admin read model, T079). Newest first."""
    limit = max(1, min(limit, MAX_REPORTS_LIMIT))
    offset = max(0, offset)
    stmt = select(FamilyReport, Elder.name).outerjoin(Elder, FamilyReport.elder_id == Elder.id)
    if elder_id is not None:
        stmt = stmt.where(FamilyReport.elder_id == elder_id)
    stmt = (
        stmt.order_by(FamilyReport.period_month.desc(), FamilyReport.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    return [(row[0], row[1]) for row in result.all()]
