import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import get_tenant_db, require_admin_role, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.db.models import CallSchedule
from usan_api.repositories import admin_audit
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
    prefix="/v1/admin/schedules",
    tags=["admin-schedules"],
    dependencies=[Depends(require_admin_session)],
)


async def _get_or_404(db: AsyncSession, schedule_id: uuid.UUID) -> CallSchedule:
    schedule = await schedules_repo.get_schedule(db, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    return schedule


@router.get("", response_model=list[ScheduleResponse])
async def list_schedules(
    contact_id: uuid.UUID | None = None,
    slot: Slot | None = None,
    last_result: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_tenant_db),
) -> list[ScheduleResponse]:
    rows = await schedules_repo.list_schedules_with_contact_name(
        db, contact_id=contact_id, slot=slot, last_result=last_result, limit=limit, offset=offset
    )
    return [ScheduleResponse.from_model(s, contact_name=name) for s, name in rows]


@router.get("/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(
    schedule_id: uuid.UUID, db: AsyncSession = Depends(get_tenant_db)
) -> ScheduleResponse:
    schedule = await _get_or_404(db, schedule_id)
    contact = await contacts_repo.get_contact(db, schedule.contact_id)
    return ScheduleResponse.from_model(schedule, contact_name=contact.name if contact else None)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ScheduleResponse)
async def create_schedule(
    body: CreateScheduleRequest,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ScheduleResponse:
    contact = await contacts_repo.get_contact(db, body.contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="contact not found")
    try:
        schedule = await schedules_service.build_create(db, body=body, contact=contact)
        await admin_audit.record(
            db,
            actor_email=actor,
            action="schedule.create",
            entity_type="schedule",
            entity_id=str(schedule.id),
            detail={"contact_id": str(body.contact_id), "slot": body.slot},
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail=f"contact already has a {body.slot} schedule"
        ) from exc
    await db.refresh(schedule)
    return ScheduleResponse.from_model(schedule, contact_name=contact.name)


@router.patch("/{schedule_id}", response_model=ScheduleResponse)
async def update_schedule(
    schedule_id: uuid.UUID,
    body: UpdateScheduleRequest,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ScheduleResponse:
    schedule = await _get_or_404(db, schedule_id)
    contact = await contacts_repo.get_contact(db, schedule.contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="contact not found")
    await schedules_service.apply_update(db, schedule=schedule, body=body, contact=contact)
    await admin_audit.record(
        db,
        actor_email=actor,
        action="schedule.update",
        entity_type="schedule",
        entity_id=str(schedule_id),
        detail={"fields": sorted(body.model_dump(exclude_unset=True).keys())},
    )
    await db.commit()
    await db.refresh(schedule)
    return ScheduleResponse.from_model(schedule, contact_name=contact.name)


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: uuid.UUID,
    db: AsyncSession = Depends(get_tenant_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> None:
    schedule = await _get_or_404(db, schedule_id)
    await schedules_repo.delete_schedule(db, schedule)
    await admin_audit.record(
        db,
        actor_email=actor,
        action="schedule.delete",
        entity_type="schedule",
        entity_id=str(schedule_id),
    )
    await db.commit()
