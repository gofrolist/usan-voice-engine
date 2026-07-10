import asyncio
import base64
import hashlib
import json
import time
import uuid

import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import service_token as _service_token
from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo


def _sign(body: str, key: str, secret: str) -> str:
    digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    now = int(time.time())
    return jwt.encode(
        {"iss": key, "nbf": now - 5, "exp": now + 60, "sha256": digest},
        secret,
        algorithm="HS256",
    )


async def _seed_in_progress(url, room):
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    try:
        async with factory() as db:
            contact = await contacts_repo.create_contact(
                db, name="A", phone_e164=phone, timezone="UTC"
            )
            call = await calls_repo.create_call(
                db,
                contact_id=contact.id,
                direction=CallDirection.OUTBOUND,
                status=CallStatus.DIALING,
                livekit_room=room,
            )
            await calls_repo.mark_answered(db, call.id, sip_call_id="SCL")  # -> IN_PROGRESS
            await db.commit()
            return call.id
    finally:
        await engine.dispose()


def test_end_call_schedules_flush_once(client, async_database_url, monkeypatch):
    from usan_api.routers import tools as tools_router

    seen: list = []

    async def _recorder(call_id):
        seen.append(call_id)

    # Monkeypatch where it is IMPORTED (F6).
    monkeypatch.setattr(tools_router, "flush_pending_sms", _recorder)

    room = f"usan-outbound-{uuid.uuid4()}"
    call_id = asyncio.run(_seed_in_progress(async_database_url, room))
    r = client.post(
        "/v1/tools/end_call",
        json={"call_id": str(call_id), "reason": "check_in_complete"},
        headers={"Authorization": f"Bearer {_service_token(str(call_id))}"},
    )
    assert r.status_code == 200
    assert seen == [call_id]  # scheduled EXACTLY once (the IN_PROGRESS->COMPLETED transition)


def test_end_call_idempotent_replay_does_not_schedule_again(
    client, async_database_url, monkeypatch
):
    from usan_api.routers import tools as tools_router

    seen: list = []

    async def _recorder(call_id):
        seen.append(call_id)

    monkeypatch.setattr(tools_router, "flush_pending_sms", _recorder)
    room = f"usan-outbound-{uuid.uuid4()}"
    call_id = asyncio.run(_seed_in_progress(async_database_url, room))
    hdr = {"Authorization": f"Bearer {_service_token(str(call_id))}"}
    client.post("/v1/tools/end_call", json={"call_id": str(call_id), "reason": "x"}, headers=hdr)
    client.post("/v1/tools/end_call", json={"call_id": str(call_id), "reason": "x"}, headers=hdr)
    # Second call is an idempotent no-op (updated is None) -> no extra schedule.
    assert seen == [call_id]


def test_room_finished_schedules_flush_once(client, async_database_url, monkeypatch):
    from usan_api.routers import webhooks as wh_router

    seen: list = []

    async def _recorder(call_id):
        seen.append(call_id)

    monkeypatch.setattr(wh_router, "flush_pending_sms", _recorder)
    room = f"usan-outbound-{uuid.uuid4()}"
    call_id = asyncio.run(_seed_in_progress(async_database_url, room))
    body = json.dumps(
        {
            "event": "room_finished",
            "room": {"name": room},
            "id": "ev1",
            "createdAt": int(time.time()),
        }
    )
    token = _sign(body, "key", "a" * 32)
    r = client.post(
        "/webhooks/livekit",
        content=body,
        headers={"Authorization": token, "Content-Type": "application/webhook+json"},
    )
    assert r.status_code == 200
    assert seen == [call_id]
