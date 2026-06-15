"""Operator CRUD for per-elder call schedules (spec §4.1).

PHI note (spec §8): schedule ``dynamic_vars`` are LIVE re-used config exempt
from retention; the operator PHI-removal path is PATCH (clear the vars) or
DELETE. Audit log lines bind ids and the client IP only — never the elder's
name or the vars themselves.

Timezone handling is fail-closed: ``schedule_windows.next_run_at`` raises
``ValueError`` on an unresolvable elder timezone (or a quiet-hours-empty
window) and every compute site here maps that to 422 — including PATCH, where
the elder's timezone may have gone bad after the schedule was created (the
elder API only length-validates it).
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import require_operator_token
from usan_api.client_ip import client_ip
from usan_api.db.models import CallSchedule
from usan_api.db.session import get_db
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import call_schedules as schedules_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schedule_windows import days_to_mask, next_run_at
from usan_api.schemas.schedule import (
    CreateScheduleRequest,
    ScheduleResponse,
    Slot,
    UpdateScheduleRequest,
)

router = APIRouter(
    prefix="/v1/schedules",
    tags=["schedules"],
    dependencies=[Depends(require_operator_token)],
)


def _audit(request: Request, schedule_id: uuid.UUID, action: str, **extra: str) -> None:
    """Mutation audit line (spec §4/§8): client IP + ids + action — never PHI."""
    logger.bind(
        client=client_ip(request), schedule_id=str(schedule_id), action=action, **extra
    ).info("Schedule {action}", action=action)


def _compute_next_run_at(schedule_like: CallSchedule | CreateScheduleRequest, tz: str) -> datetime:
    """next_run_at from now for the (merged) window/days; ValueError -> 422 fail-closed."""
    if isinstance(schedule_like, CreateScheduleRequest):
        days_mask = schedule_like.days_mask
    else:
        days_mask = schedule_like.days_of_week
    try:
        computed = next_run_at(
            datetime.now(UTC),
            tz,
            window_start=schedule_like.window_start_local,
            window_end=schedule_like.window_end_local,
            days_mask=days_mask,
        )
        if computed is None:
            # Defensive: None is policy-induced only (§3.3.3 rule 2) and this
            # router never passes policy bounds — if ever reached, fail closed
            # through the SAME handled 422 path as the other ValueErrors above,
            # never an unhandled 500.
            raise ValueError("schedule window produced no dialable time")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return computed


async def _require_live_override(db: AsyncSession, profile_id: uuid.UUID) -> None:
    """422 unless the override would actually take effect (ACTIVE + published, spec §4)."""
    if not await agent_profiles_repo.is_live_profile(db, profile_id):
        raise HTTPException(
            status_code=422,
            detail="profile_override must reference an active profile with a published version",
        )


async def _get_or_404(db: AsyncSession, schedule_id: uuid.UUID) -> CallSchedule:
    schedule = await schedules_repo.get_schedule(db, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    return schedule


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ScheduleResponse)
async def create_schedule(
    body: CreateScheduleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ScheduleResponse:
    elder = await elders_repo.get_elder(db, body.elder_id)
    if elder is None:
        raise HTTPException(status_code=404, detail="elder not found")
    if (
        await schedules_repo.get_by_elder_slot(db, elder_id=body.elder_id, slot=body.slot)
        is not None
    ):
        raise HTTPException(status_code=409, detail=f"elder already has a {body.slot} schedule")
    if body.profile_override is not None:
        await _require_live_override(db, body.profile_override)
    computed = _compute_next_run_at(body, elder.timezone)
    try:
        schedule = await schedules_repo.create_schedule(
            db,
            elder_id=body.elder_id,
            slot=body.slot,
            window_start_local=body.window_start_local,
            window_end_local=body.window_end_local,
            days_of_week=body.days_mask,
            enabled=body.enabled,
            dynamic_vars=body.dynamic_vars,
            profile_override=body.profile_override,
            next_run_at=computed,
        )
        await db.commit()
    except IntegrityError as exc:
        # Race fallback for the UNIQUE(elder_id, slot) pre-check above.
        await db.rollback()
        raise HTTPException(
            status_code=409, detail=f"elder already has a {body.slot} schedule"
        ) from exc
    _audit(request, schedule.id, "schedule_created", elder_id=str(body.elder_id))
    return ScheduleResponse.from_model(schedule)


@router.get("", response_model=list[ScheduleResponse])
async def list_schedules(
    elder_id: uuid.UUID | None = None,
    slot: Slot | None = None,
    last_result: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> list[ScheduleResponse]:
    # ?last_result=skipped_window is the "who missed today's call" view (spec §4.1);
    # ?slot=evening narrows to one slot (US5). The repository clamps limit/offset to
    # the bounded-read house rules.
    rows = await schedules_repo.list_schedules(
        db, elder_id=elder_id, slot=slot, last_result=last_result, limit=limit, offset=offset
    )
    return [ScheduleResponse.from_model(s) for s in rows]


@router.get("/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(
    schedule_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> ScheduleResponse:
    return ScheduleResponse.from_model(await _get_or_404(db, schedule_id))


@router.patch("/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule(
    schedule_id: uuid.UUID,
    body: UpdateScheduleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ScheduleResponse:
    schedule = await _get_or_404(db, schedule_id)
    elder = await elders_repo.get_elder(db, schedule.elder_id)
    if elder is None:  # CASCADE makes this unreachable in practice; fail closed anyway.
        raise HTTPException(status_code=404, detail="elder not found")

    # Merge (window fields travel together — schema-enforced) then revalidate the
    # merged state: override liveness, and window/days/tz via the recompute below.
    if body.enabled is not None:
        schedule.enabled = body.enabled
    if body.window_start_local is not None and body.window_end_local is not None:
        schedule.window_start_local = body.window_start_local
        schedule.window_end_local = body.window_end_local
    if body.days_of_week is not None:
        schedule.days_of_week = days_to_mask(body.days_of_week)
    if body.dynamic_vars is not None:
        schedule.dynamic_vars = body.dynamic_vars
    if "profile_override" in body.model_fields_set:  # explicit null clears the override
        if body.profile_override is not None:
            await _require_live_override(db, body.profile_override)
        schedule.profile_override = body.profile_override

    schedule.next_run_at = _compute_next_run_at(schedule, elder.timezone)
    await db.commit()
    # updated_at is server-generated (onupdate=func.now()), so the flushed UPDATE
    # expired it; refresh before serializing or the sync read raises MissingGreenlet.
    await db.refresh(schedule)
    _audit(request, schedule.id, "schedule_updated")
    return ScheduleResponse.from_model(schedule)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> None:
    schedule = await _get_or_404(db, schedule_id)
    await schedules_repo.delete_schedule(db, schedule)
    await db.commit()
    _audit(request, schedule_id, "schedule_deleted")
