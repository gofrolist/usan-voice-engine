"""Operator CRUD for per-contact call schedules (spec §4.1).

PHI note (spec §8): schedule ``dynamic_vars`` are LIVE re-used config exempt
from retention; the operator PHI-removal path is PATCH (clear the vars) or
DELETE. Audit log lines bind ids and the client IP only — never the contact's
name or the vars themselves.

Timezone handling is fail-closed: ``schedule_windows.next_run_at`` raises
``ValueError`` on an unresolvable contact timezone (or a quiet-hours-empty
window) and every compute site here maps that to 422 — including PATCH, where
the contact's timezone may have gone bad after the schedule was created (the
contact API only length-validates it).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import require_operator_token
from usan_api.client_ip import client_ip
from usan_api.db.models import CallSchedule
from usan_api.db.session import get_db
from usan_api.repositories import call_schedules as schedules_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.schemas.schedule import (
    CreateScheduleRequest,
    ScheduleResponse,
    Slot,
    UpdateScheduleRequest,
)
from usan_api.services import schedules as schedules_service

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
    contact = await contacts_repo.get_contact(db, body.contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="contact not found")
    try:
        schedule = await schedules_service.build_create(db, body=body, contact=contact)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail=f"contact already has a {body.slot} schedule"
        ) from exc
    _audit(request, schedule.id, "schedule_created", contact_id=str(body.contact_id))
    return ScheduleResponse.from_model(schedule)


@router.get("", response_model=list[ScheduleResponse])
async def list_schedules(
    contact_id: uuid.UUID | None = None,
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
        db, contact_id=contact_id, slot=slot, last_result=last_result, limit=limit, offset=offset
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
    contact = await contacts_repo.get_contact(db, schedule.contact_id)
    if contact is None:  # CASCADE makes this unreachable in practice; fail closed anyway.
        raise HTTPException(status_code=404, detail="contact not found")

    await schedules_service.apply_update(db, schedule=schedule, body=body, contact=contact)
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
