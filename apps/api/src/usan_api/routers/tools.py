import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import require_service_token
from usan_api.db.models import Call
from usan_api.db.session import get_db
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import wellness as wellness_repo
from usan_api.schemas.tools import LoggedResponse, LogWellnessRequest

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
