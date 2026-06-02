import asyncio
import datetime
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


async def _seed_ended_with_egress(
    async_database_url: str,
    room: str,
    *,
    ended_at: datetime.datetime,
    egress_id: str = "EG",
    recording_uri: str | None = None,
    recording_status: str | None = None,
) -> uuid.UUID:
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
                status=CallStatus.COMPLETED,
                livekit_room=room,
            )
            call.egress_id = egress_id
            call.ended_at = ended_at
            call.recording_uri = recording_uri
            call.recording_status = recording_status
            await db.commit()
            return call.id
    finally:
        await engine.dispose()


async def _reconcile(async_database_url, *, now, grace_s):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            ids = await calls_repo.reconcile_missing_recordings(
                db, now=now, grace_s=grace_s, limit=50
            )
            await db.commit()
            return ids
    finally:
        await engine.dispose()


async def _read(async_database_url, call_id):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            return await calls_repo.get_call(db, call_id)
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
    assert call.recording_status == "complete"  # set alongside the URI


def test_set_egress_id_unknown_room_returns_none(client, async_database_url):
    call = asyncio.run(_apply(async_database_url, "no-such-room", egress_id="EG_x"))
    assert call is None


_NOW = datetime.datetime(2026, 6, 2, 12, 0, tzinfo=datetime.UTC)


def test_reconcile_flags_stranded_recording(client, async_database_url):
    # Egress started, room ended 10min ago, no webhook ever arrived.
    room = "usan-outbound-strand1"
    call_id = asyncio.run(
        _seed_ended_with_egress(
            async_database_url, room, ended_at=_NOW - datetime.timedelta(seconds=600)
        )
    )
    flagged = asyncio.run(_reconcile(async_database_url, now=_NOW, grace_s=300))
    assert call_id in flagged
    # Idempotent: recording_status is now set, so a second pass does not re-flag it.
    again = asyncio.run(_reconcile(async_database_url, now=_NOW, grace_s=300))
    assert call_id not in again
    final = asyncio.run(_read(async_database_url, call_id))
    assert final.recording_status == "missing"
    assert final.recording_uri is None


def test_reconcile_skips_recent_and_recorded(client, async_database_url):
    recent = asyncio.run(
        _seed_ended_with_egress(
            async_database_url,
            "usan-outbound-recent",
            ended_at=_NOW - datetime.timedelta(seconds=60),  # within grace
        )
    )
    recorded = asyncio.run(
        _seed_ended_with_egress(
            async_database_url,
            "usan-outbound-recorded",
            ended_at=_NOW - datetime.timedelta(seconds=600),
            recording_uri="gs://b/x.ogg",  # already has a recording
        )
    )
    flagged = asyncio.run(_reconcile(async_database_url, now=_NOW, grace_s=300))
    assert recent not in flagged
    assert recorded not in flagged
