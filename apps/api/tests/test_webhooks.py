import base64
import hashlib
import time

import jwt

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo


def _sign(body: str, key: str, secret: str) -> str:
    digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    now = int(time.time())
    claims = {"iss": key, "nbf": now - 5, "exp": now + 60, "sha256": digest}
    return jwt.encode(claims, secret, algorithm="HS256")


def _room_finished(room: str) -> str:
    return f'{{"event":"room_finished","room":{{"name":"{room}"}},"id":"ev1","createdAt":1}}'


async def _make_in_progress_call(async_database_url, room: str):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    factory = async_sessionmaker(
        create_async_engine(async_database_url, poolclass=NullPool), expire_on_commit=False
    )
    async with factory() as db:
        elder = await elders_repo.create_elder(
            db, name="A", phone_e164="+15551112222", timezone="UTC"
        )
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.DIALING,
            livekit_room=room,
        )
        await calls_repo.mark_answered(db, call.id, sip_call_id="SCL")
        await db.commit()
        return call.id


def test_livekit_webhook_room_finished_completes_call(client, async_database_url):
    import asyncio

    room = "usan-outbound-wh1"
    call_id = asyncio.run(_make_in_progress_call(async_database_url, room))
    body = _room_finished(room)
    token = _sign(body, "key", "a" * 32)
    r = client.post(
        "/webhooks/livekit",
        content=body,
        headers={"Authorization": token, "Content-Type": "application/webhook+json"},
    )
    assert r.status_code == 200
    follow = client.get(f"/v1/calls/{call_id}")
    assert follow.json()["status"] == "completed"


def test_livekit_webhook_bad_signature_rejected(client):
    body = _room_finished("usan-outbound-x")
    r = client.post(
        "/webhooks/livekit",
        content=body,
        headers={"Authorization": "not-a-valid-token"},
    )
    assert r.status_code == 401


def test_livekit_webhook_unknown_event_ignored(client):
    body = '{"event":"track_published","room":{"name":"r"},"id":"e","createdAt":1}'
    token = _sign(body, "key", "a" * 32)
    r = client.post("/webhooks/livekit", content=body, headers={"Authorization": token})
    assert r.status_code == 200
