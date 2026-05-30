from fastapi import APIRouter, Depends, HTTPException, Request, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import livekit_webhooks
from usan_api.db.session import get_db
from usan_api.repositories import calls as calls_repo
from usan_api.settings import Settings, get_settings

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Webhook event strings that signal a room (and thus the call) has ended.
_ROOM_END_EVENTS = frozenset({"room_finished"})


@router.post("/livekit", status_code=status.HTTP_200_OK)
async def livekit_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    body = (await request.body()).decode("utf-8")
    auth = request.headers.get("Authorization", "")
    try:
        event = livekit_webhooks.verify_livekit_webhook(body, auth, settings)
    except Exception as exc:  # invalid signature / hash mismatch / malformed
        raise HTTPException(status_code=401, detail="invalid webhook signature") from exc

    if event.event in _ROOM_END_EVENTS and event.room and event.room.name:
        call = await calls_repo.mark_completed_if_in_progress(db, event.room.name)
        if call is not None:
            await db.commit()
            logger.bind(call_id=str(call.id), room=event.room.name).info(
                "Call completed via room_finished webhook"
            )
    return {"ok": True}
