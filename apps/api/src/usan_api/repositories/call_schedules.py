"""Repository for `call_schedules` rows (recurring per-elder wellness calls).

House rules: functions take the request session, `flush()` (+`refresh()` for
returned rows), and never commit — routers and the scheduler poller own the
transaction boundary.
"""

import uuid
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import CallSchedule

# Bound the list read (house pattern: sibling repos cap at 500); newest-first
# with an id tiebreaker so the cap keeps the most recent (spec §4.1).
MAX_SCHEDULES_LIMIT = 500


async def create_schedule(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID,
    window_start_local: time,
    window_end_local: time,
    days_of_week: int,
    enabled: bool,
    dynamic_vars: dict[str, Any],
    profile_override: uuid.UUID | None,
    next_run_at: datetime,
    slot: str = "morning",
) -> CallSchedule:
    """Insert a schedule. UNIQUE (elder_id, slot) raises IntegrityError on a
    duplicate (one schedule per elder per morning|evening slot — the router maps it
    to 409). ``slot`` defaults to 'morning' so pre-US5 single-slot callers are
    unchanged.
    """
    row = CallSchedule(
        elder_id=elder_id,
        slot=slot,
        window_start_local=window_start_local,
        window_end_local=window_end_local,
        days_of_week=days_of_week,
        enabled=enabled,
        dynamic_vars=dynamic_vars,
        profile_override=profile_override,
        next_run_at=next_run_at,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def get_schedule(db: AsyncSession, schedule_id: uuid.UUID) -> CallSchedule | None:
    return await db.get(CallSchedule, schedule_id)


async def get_by_elder(db: AsyncSession, elder_id: uuid.UUID) -> list[CallSchedule]:
    """All of an elder's schedules — one row per slot (US5), ordered by slot for a
    stable read."""
    result = await db.execute(
        select(CallSchedule).where(CallSchedule.elder_id == elder_id).order_by(CallSchedule.slot)
    )
    return list(result.scalars().all())


async def get_by_elder_slot(
    db: AsyncSession, *, elder_id: uuid.UUID, slot: str
) -> CallSchedule | None:
    """The elder's schedule for one slot — the router's per-(elder, slot) 409
    pre-check; the composite UNIQUE(elder_id, slot) is the race backstop."""
    result = await db.execute(
        select(CallSchedule).where(CallSchedule.elder_id == elder_id, CallSchedule.slot == slot)
    )
    return result.scalar_one_or_none()


async def list_schedules(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID | None = None,
    slot: str | None = None,
    last_result: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[CallSchedule]:
    """Most-recent schedules, optionally filtered by elder/slot/last_result.

    `?last_result=skipped_window` is the operator's "who missed today's call?"
    view (spec §4.1); `?slot=evening` narrows to one slot (US5). Newest first with
    an `id` tiebreaker; `limit` clamped to 1..MAX_SCHEDULES_LIMIT.
    """
    limit = max(1, min(limit, MAX_SCHEDULES_LIMIT))
    offset = max(0, offset)
    stmt = select(CallSchedule)
    if elder_id is not None:
        stmt = stmt.where(CallSchedule.elder_id == elder_id)
    if slot is not None:
        stmt = stmt.where(CallSchedule.slot == slot)
    if last_result is not None:
        stmt = stmt.where(CallSchedule.last_result == last_result)
    stmt = (
        stmt.order_by(CallSchedule.created_at.desc(), CallSchedule.id.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def delete_schedule(db: AsyncSession, schedule: CallSchedule) -> None:
    await db.delete(schedule)
    await db.flush()


async def claim_due_schedules(db: AsyncSession, *, now: datetime, limit: int) -> list[CallSchedule]:
    """Lock and return up to ``limit`` due schedules, earliest ``next_run_at`` first.

    `WHERE enabled AND next_run_at <= now` is the exact `idx_call_schedules_due`
    partial-index predicate; FOR UPDATE SKIP LOCKED lets concurrent pollers claim
    disjoint rows without blocking. Rows stay locked until the caller's
    transaction ends, so the caller must process and commit one cycle promptly.
    """
    result = await db.execute(
        select(CallSchedule)
        .where(CallSchedule.enabled, CallSchedule.next_run_at <= now)
        .order_by(CallSchedule.next_run_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(result.scalars().all())


async def record_result(
    db: AsyncSession,
    schedule: CallSchedule,
    *,
    result: str,
    now: datetime,
    next_run_at: datetime | None = None,
    last_materialized_date: date | None = None,
    enabled: bool | None = None,
) -> None:
    """Write per-decision bookkeeping (spec §4.1/§5.2): every materialization
    branch records ``last_result``/``last_result_at``; optional kwargs advance
    ``next_run_at``, stamp ``last_materialized_date``, or flip ``enabled``
    (``enabled=False`` is the DNC auto-disable write path, spec §5.3).
    """
    schedule.last_result = result
    schedule.last_result_at = now
    if next_run_at is not None:
        schedule.next_run_at = next_run_at
    if last_materialized_date is not None:
        schedule.last_materialized_date = last_materialized_date
    if enabled is not None:
        schedule.enabled = enabled
    await db.flush()
