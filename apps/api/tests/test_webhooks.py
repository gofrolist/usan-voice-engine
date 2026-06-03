import asyncio
import base64
import hashlib
import json
import time
import uuid
from types import SimpleNamespace

import jwt
from livekit import api
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.routers.webhooks import _recording_uri

# Operator bearer token for the management plane (matches conftest's OPERATOR_API_KEY).
_OP = {"Authorization": "Bearer " + "o" * 32}


def _sign(body: str, key: str, secret: str) -> str:
    digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    now = int(time.time())
    claims = {"iss": key, "nbf": now - 5, "exp": now + 60, "sha256": digest}
    return jwt.encode(claims, secret, algorithm="HS256")


def _event(event: str, room: str, *, created_at: int | None = None) -> str:
    when = int(time.time()) if created_at is None else created_at
    return json.dumps({"event": event, "room": {"name": room}, "id": "ev1", "createdAt": when})


async def _seed_call(async_database_url, room, *, status, answered=False):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    try:
        async with factory() as db:
            elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
            call = await calls_repo.create_call(
                db,
                elder_id=elder.id,
                direction=CallDirection.OUTBOUND,
                status=status,
                livekit_room=room,
            )
            if answered:
                await calls_repo.mark_answered(db, call.id, sip_call_id="SCL")
            await db.commit()
            return call.id
    finally:
        await engine.dispose()


def test_livekit_webhook_room_finished_completes_call(client, async_database_url):
    room = "usan-outbound-wh1"
    call_id = asyncio.run(
        _seed_call(async_database_url, room, status=CallStatus.DIALING, answered=True)
    )
    body = _event("room_finished", room)
    token = _sign(body, "key", "a" * 32)
    r = client.post(
        "/webhooks/livekit",
        content=body,
        headers={"Authorization": token, "Content-Type": "application/webhook+json"},
    )
    assert r.status_code == 200
    follow = client.get(f"/v1/calls/{call_id}", headers=_OP)
    assert follow.json()["status"] == "completed"


def test_livekit_webhook_does_not_complete_terminal_call(client, async_database_url):
    # room_finished for a call that already reached a terminal state (no_answer)
    # must be a no-op — the in_progress gate protects it.
    room = "usan-outbound-wh2"
    call_id = asyncio.run(_seed_call(async_database_url, room, status=CallStatus.NO_ANSWER))
    body = _event("room_finished", room)
    token = _sign(body, "key", "a" * 32)
    r = client.post("/webhooks/livekit", content=body, headers={"Authorization": token})
    assert r.status_code == 200
    follow = client.get(f"/v1/calls/{call_id}", headers=_OP)
    assert follow.json()["status"] == "no_answer"


def test_livekit_webhook_bad_signature_rejected(client):
    body = _event("room_finished", "usan-outbound-x")
    r = client.post(
        "/webhooks/livekit",
        content=body,
        headers={"Authorization": "not-a-valid-token"},
    )
    assert r.status_code == 401


def test_livekit_webhook_unknown_event_ignored(client):
    body = _event("track_published", "r")
    token = _sign(body, "key", "a" * 32)
    r = client.post("/webhooks/livekit", content=body, headers={"Authorization": token})
    assert r.status_code == 200


def test_livekit_webhook_stale_event_rejected(client):
    # A signature-valid but ancient (replayed) delivery is rejected with 400.
    body = _event("room_finished", "usan-outbound-stale", created_at=1)
    token = _sign(body, "key", "a" * 32)
    r = client.post("/webhooks/livekit", content=body, headers={"Authorization": token})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# _recording_uri unit tests
# ---------------------------------------------------------------------------


def _egress_info(status, files):
    return SimpleNamespace(status=status, file_results=files)


def test_recording_uri_complete_with_bucket():
    info = _egress_info(
        api.EgressStatus.EGRESS_COMPLETE,
        [SimpleNamespace(filename="recordings/2026-06-02/x.ogg", location="gs://orig/x")],
    )
    assert _recording_uri(info, "bkt") == "gs://bkt/recordings/2026-06-02/x.ogg"


def test_recording_uri_complete_without_bucket_uses_location():
    info = _egress_info(
        api.EgressStatus.EGRESS_COMPLETE,
        [SimpleNamespace(filename="recordings/x.ogg", location="gs://orig/x.ogg")],
    )
    assert _recording_uri(info, None) == "gs://orig/x.ogg"


def test_recording_uri_failed_returns_none():
    info = _egress_info(
        api.EgressStatus.EGRESS_FAILED,
        [SimpleNamespace(filename="x", location="y")],
    )
    assert _recording_uri(info, "bkt") is None


def test_recording_uri_no_files_returns_none():
    info = _egress_info(api.EgressStatus.EGRESS_COMPLETE, [])
    assert _recording_uri(info, "bkt") is None


# ---------------------------------------------------------------------------
# egress webhook integration tests
# ---------------------------------------------------------------------------


def _egress_event(
    event,
    room,
    *,
    egress_id="EG1",
    status="EGRESS_COMPLETE",
    filename="recordings/2026-06-02/x.ogg",
    location="gs://b/recordings/2026-06-02/x.ogg",
):
    info = {"egressId": egress_id, "roomName": room, "status": status}
    if event == "egress_ended":
        info["fileResults"] = [{"filename": filename, "location": location}]
    return json.dumps(
        {"event": event, "egressInfo": info, "id": "ev1", "createdAt": int(time.time())}
    )


async def _read_call(async_database_url, call_id):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            return await calls_repo.get_call(db, call_id)
    finally:
        await engine.dispose()


def test_livekit_webhook_egress_started_sets_egress_id(client, async_database_url):
    room = "usan-outbound-egs"
    call_id = asyncio.run(
        _seed_call(async_database_url, room, status=CallStatus.IN_PROGRESS, answered=True)
    )
    body = _egress_event("egress_started", room, egress_id="EG_42")
    token = _sign(body, "key", "a" * 32)
    r = client.post("/webhooks/livekit", content=body, headers={"Authorization": token})
    assert r.status_code == 200
    call = asyncio.run(_read_call(async_database_url, call_id))
    assert call.egress_id == "EG_42"


def test_livekit_webhook_egress_ended_stores_recording_uri(client, async_database_url):
    room = "usan-outbound-ege"
    call_id = asyncio.run(
        _seed_call(async_database_url, room, status=CallStatus.IN_PROGRESS, answered=True)
    )
    body = _egress_event("egress_ended", room, location="gs://b/recordings/2026-06-02/x.ogg")
    token = _sign(body, "key", "a" * 32)
    r = client.post("/webhooks/livekit", content=body, headers={"Authorization": token})
    assert r.status_code == 200
    call = asyncio.run(_read_call(async_database_url, call_id))
    assert call.recording_uri == "gs://b/recordings/2026-06-02/x.ogg"
    assert call.recording_status == "complete"


def test_livekit_webhook_egress_ended_failed_stores_no_recording(client, async_database_url):
    room = "usan-outbound-egf"
    call_id = asyncio.run(
        _seed_call(async_database_url, room, status=CallStatus.IN_PROGRESS, answered=True)
    )
    body = _egress_event("egress_ended", room, status="EGRESS_FAILED")
    token = _sign(body, "key", "a" * 32)
    r = client.post("/webhooks/livekit", content=body, headers={"Authorization": token})
    assert r.status_code == 200
    call = asyncio.run(_read_call(async_database_url, call_id))
    assert call.recording_uri is None
    assert call.recording_status == "failed"
