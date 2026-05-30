import asyncio
import base64
import hashlib
import json
import time
import uuid

import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo


def _sign(body: str, key: str, secret: str) -> str:
    digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    now = int(time.time())
    claims = {"iss": key, "nbf": now - 5, "exp": now + 60, "sha256": digest}
    return jwt.encode(claims, secret, algorithm="HS256")


def _event(event: str, room: str) -> str:
    return json.dumps({"event": event, "room": {"name": room}, "id": "ev1", "createdAt": 1})


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
    follow = client.get(f"/v1/calls/{call_id}")
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
    follow = client.get(f"/v1/calls/{call_id}")
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
