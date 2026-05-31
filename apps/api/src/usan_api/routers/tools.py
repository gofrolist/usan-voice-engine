import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import require_service_token
from usan_api.db.models import Call
from usan_api.db.session import get_db
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import medications as medications_repo
from usan_api.repositories import wellness as wellness_repo
from usan_api.schemas.tools import (
    CallEndedResponse,
    EndCallRequest,
    GetTodayMedsRequest,
    LoggedResponse,
    LogMedicationRequest,
    LogWellnessRequest,
    MedicationScheduleItem,
    TodayMedsResponse,
)

router = APIRouter(prefix="/v1/tools", tags=["tools"])


async def _authorize_call(call_id: uuid.UUID, claims: dict[str, Any], db: AsyncSession) -> Call:
    """Verify the JWT is scoped to this call and load it (404 if unknown)."""
    if claims.get("call_id") != str(call_id):
        raise HTTPException(status_code=403, detail="token not valid for this call")
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    return call


def _require_elder(call: Call) -> uuid.UUID:
    if call.elder_id is None:
        raise HTTPException(status_code=409, detail="call has no associated elder")
    return call.elder_id


@router.post("/log_wellness", response_model=LoggedResponse)
async def log_wellness(
    body: LogWellnessRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> LoggedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    elder_id = _require_elder(call)
    row = await wellness_repo.create_wellness_log(
        db,
        call_id=call.id,
        elder_id=elder_id,
        mood=body.mood,
        pain_level=body.pain_level,
        notes=body.notes,
    )
    await db.commit()
    logger.bind(call_id=str(call.id)).info("Logged wellness")
    return LoggedResponse(id=row.id)


@router.post("/log_medication", response_model=LoggedResponse)
async def log_medication(
    body: LogMedicationRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> LoggedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    elder_id = _require_elder(call)
    row = await medications_repo.create_medication_log(
        db,
        call_id=call.id,
        elder_id=elder_id,
        medication_name=body.medication_name,
        taken=body.taken,
        reported_time=body.reported_time,
    )
    await db.commit()
    logger.bind(call_id=str(call.id)).info("Logged medication")
    return LoggedResponse(id=row.id)


@router.post("/get_today_meds", response_model=TodayMedsResponse)
async def get_today_meds(
    body: GetTodayMedsRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> TodayMedsResponse:
    call = await _authorize_call(body.call_id, claims, db)
    elder_id = _require_elder(call)
    elder = await elders_repo.get_elder(db, elder_id)
    if elder is None:
        raise HTTPException(status_code=409, detail="elder record not found")
    raw = elder.meta.get("medication_schedule", [])
    items: list[MedicationScheduleItem] = []
    if isinstance(raw, list):
        for entry in raw:
            try:
                items.append(MedicationScheduleItem.model_validate(entry))
            except ValidationError:
                logger.bind(elder_id=str(elder_id)).warning("Skipping malformed medication entry")
    return TodayMedsResponse(medications=items)


@router.post("/end_call", response_model=CallEndedResponse)
async def end_call(
    body: EndCallRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> CallEndedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    updated = await calls_repo.complete_call_if_in_progress(db, call.id, end_reason=body.reason)
    await db.commit()
    logger.bind(call_id=str(call.id), reason=body.reason).info("end_call requested")
    return CallEndedResponse(status=(updated or call).status.value)
