"""Start an audio-only room-composite egress to GCS for the call recording (spec §9).

Best-effort: failing to start recording must never break a live call. The separate
LiveKit egress worker uploads to GCS using its Application Default Credentials, so no
key is shipped from here — the request carries the bucket and an empty credentials
string. RoomComposite egress auto-stops when the room closes, so there is nothing to
stop on hangup (the agent already deletes the room).
"""

import datetime
import uuid
from typing import Any, cast

from livekit import api
from loguru import logger

from usan_agent.ids import validate_call_id
from usan_agent.settings import Settings


def _http_url(ws_url: str) -> str:
    if ws_url.startswith("wss://"):
        return "https://" + ws_url[len("wss://") :]
    if ws_url.startswith("ws://"):
        return "http://" + ws_url[len("ws://") :]
    return ws_url


def recording_filepath(
    call_id: str, *, now: datetime.datetime | None = None, token: str | None = None
) -> str:
    """The GCS object key: recordings/YYYY-MM-DD/<call_id>-<token>.ogg.

    The per-attempt random token keeps the key unique so a restarted egress never
    overwrites an existing object — the write SA holds only objectCreator (create,
    not overwrite), so a same-key collision would otherwise 403 and silently drop
    the PHI recording. The API learns the real key from the egress webhook, so a
    unique name here is safe.

    Raises ValueError on a call_id that is not a safe id, so an unvalidated value can
    never be interpolated into the GCS object key (path-traversal / namespace escape).
    """
    validate_call_id(call_id)
    day = (now or datetime.datetime.now(datetime.UTC)).strftime("%Y-%m-%d")
    suffix = token or uuid.uuid4().hex[:8]
    return f"recordings/{day}/{call_id}-{suffix}.ogg"


async def start_call_recording(ctx: Any, call_id: str, settings: Settings) -> str | None:
    """Start an audio-only OGG egress of the room to GCS. Returns the egress_id, or
    None when recording is disabled (no GCS_BUCKET) or the start failed. Never raises."""
    if not settings.gcs_bucket:
        return None
    try:
        validate_call_id(call_id)
    except ValueError:
        # Fail closed: don't record rather than write to an attacker-shaped GCS key.
        logger.bind(room=ctx.room.name).warning("Refusing to record: call_id failed validation")
        return None
    request = api.RoomCompositeEgressRequest(
        room_name=ctx.room.name,
        audio_only=True,
        file_outputs=[
            api.EncodedFileOutput(
                file_type=api.EncodedFileType.OGG,
                filepath=recording_filepath(call_id),
                gcp=api.GCPUpload(bucket=settings.gcs_bucket, credentials=""),
            )
        ],
    )
    try:
        async with api.LiveKitAPI(
            url=_http_url(settings.livekit_url),
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        ) as lkapi:
            info = await lkapi.egress.start_room_composite_egress(request)
    except Exception:
        logger.bind(call_id=call_id, room=ctx.room.name).warning("Failed to start call recording")
        return None
    egress_id = cast(str, info.egress_id)
    logger.bind(call_id=call_id, room=ctx.room.name, egress_id=egress_id).info(
        "Call recording egress started"
    )
    return egress_id
