"""Shared call-schedule validation + create/update core.

The operator ``/v1/schedules`` router and the admin ``/v1/admin/schedules`` router
both delegate here so window/quiet-hours/slot/override rules cannot drift between
the two planes. Callers own the surrounding transaction (commit + IntegrityError
handling) so the mutation and its audit row land in one commit.
"""

from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import CallSchedule, Contact
from usan_api.repositories import call_schedules as schedules_repo
from usan_api.schedule_windows import days_to_mask, next_run_at
from usan_api.schemas.schedule import CreateScheduleRequest, UpdateScheduleRequest
from usan_api.services.outbound_calls import require_live_override


def compute_next_run_at(schedule_like: CallSchedule | CreateScheduleRequest, tz: str) -> datetime:
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
            raise ValueError("schedule window produced no dialable time")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return computed


async def build_create(
    db: AsyncSession, *, body: CreateScheduleRequest, contact: Contact
) -> CallSchedule:
    """Validate + insert (flush, no commit). Raises 409 on slot clash, 422 on window/override."""
    if (
        await schedules_repo.get_by_contact_slot(db, contact_id=body.contact_id, slot=body.slot)
        is not None
    ):
        raise HTTPException(status_code=409, detail=f"contact already has a {body.slot} schedule")
    if body.profile_override is not None:
        await require_live_override(db, body.profile_override)
    computed = compute_next_run_at(body, contact.timezone)
    return await schedules_repo.create_schedule(
        db,
        contact_id=body.contact_id,
        slot=body.slot,
        window_start_local=body.window_start_local,
        window_end_local=body.window_end_local,
        days_of_week=body.days_mask,
        enabled=body.enabled,
        dynamic_vars=body.dynamic_vars,
        profile_override=body.profile_override,
        next_run_at=computed,
    )


async def apply_update(
    db: AsyncSession,
    *,
    schedule: CallSchedule,
    body: UpdateScheduleRequest,
    contact: Contact,
) -> None:
    """Merge the PATCH body onto ``schedule`` in place + recompute next_run_at (no commit)."""
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
            await require_live_override(db, body.profile_override)
        schedule.profile_override = body.profile_override
    schedule.next_run_at = compute_next_run_at(schedule, contact.timezone)
