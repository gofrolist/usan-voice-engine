from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from livekit import api
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import livekit_webhooks
from usan_api.db.session import get_db
from usan_api.repositories import calls as calls_repo
from usan_api.settings import Settings, get_settings

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Webhook event strings that signal a room (and thus the call) has ended.
_ROOM_END_EVENTS = frozenset({"room_finished"})


def _recording_uri(info: Any, gcs_bucket: str | None) -> str | None:
    """The gs:// URI for a completed egress, or None if it produced no usable file."""
    if info.status != api.EgressStatus.EGRESS_COMPLETE or not info.file_results:
        return None
    object_key = info.file_results[0].filename
    if gcs_bucket and object_key:
        return f"gs://{gcs_bucket}/{object_key}"
    return info.file_results[0].location or None


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
    elif event.event == "egress_started" and event.egress_info.room_name:
        info = event.egress_info
        call = await calls_repo.set_egress_id(db, info.room_name, info.egress_id)
        if call is not None:
            await db.commit()
            logger.bind(call_id=str(call.id), egress_id=info.egress_id).info(
                "Recorded egress_id via egress_started webhook"
            )
    elif event.event == "egress_ended" and event.egress_info.room_name:
        info = event.egress_info
        uri = _recording_uri(info, settings.gcs_bucket)
        if uri is None:
            logger.bind(room=info.room_name, status=int(info.status)).warning(
                "Egress ended without a usable recording"
            )
        else:
            call = await calls_repo.set_recording_uri(db, info.room_name, uri)
            if call is not None:
                await db.commit()
                logger.bind(call_id=str(call.id), recording_uri=uri).info(
                    "Stored recording_uri via egress_ended webhook"
                )
    return {"ok": True}
