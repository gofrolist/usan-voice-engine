import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo


async def _seed(async_database_url: str, room: str) -> uuid.UUID:
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
                status=CallStatus.IN_PROGRESS,
                livekit_room=room,
            )
            await db.commit()
            return call.id
    finally:
        await engine.dispose()


async def _apply(async_database_url: str, room: str, *, egress_id=None, recording_uri=None):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            if egress_id is not None:
                call = await calls_repo.set_egress_id(db, room, egress_id)
            else:
                call = await calls_repo.set_recording_uri(db, room, recording_uri)
            await db.commit()
            return call
    finally:
        await engine.dispose()


def test_set_egress_id_persists(client, async_database_url):
    room = "usan-outbound-eg1"
    asyncio.run(_seed(async_database_url, room))
    call = asyncio.run(_apply(async_database_url, room, egress_id="EG_1"))
    assert call is not None
    assert call.egress_id == "EG_1"


def test_set_recording_uri_persists(client, async_database_url):
    room = "usan-outbound-rc1"
    asyncio.run(_seed(async_database_url, room))
    call = asyncio.run(_apply(async_database_url, room, recording_uri="gs://b/x.ogg"))
    assert call is not None
    assert call.recording_uri == "gs://b/x.ogg"


def test_set_egress_id_unknown_room_returns_none(client, async_database_url):
    call = asyncio.run(_apply(async_database_url, "no-such-room", egress_id="EG_x"))
    assert call is None
