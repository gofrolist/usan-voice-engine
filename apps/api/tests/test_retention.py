import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import retention
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, Transcript
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import transcripts as transcripts_repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_call(factory, *, status: CallStatus, dynamic_vars: dict) -> uuid.UUID:
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=status,
            livekit_room=f"usan-outbound-{uuid.uuid4()}",
            dynamic_vars=dynamic_vars,
        )
        await db.commit()
        return call.id


async def _add_transcript(factory, call_id: uuid.UUID) -> None:
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    async with factory() as db:
        await transcripts_repo.create_transcript_segments(
            db,
            call_id=call_id,
            segments=[{"role": "user", "content": "PHI here", "started_at": now}],
        )
        await db.commit()


# A clock far in the future makes every freshly-created row older than the window.
_FUTURE = datetime(2099, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_purge_deletes_old_transcripts(session_factory):
    call_id = await _seed_call(session_factory, status=CallStatus.COMPLETED, dynamic_vars={})
    await _add_transcript(session_factory, call_id)
    async with session_factory() as db:
        transcripts, _ = await retention.purge_expired(db, days=30, now=_FUTURE)
        await db.commit()
    assert transcripts == 1
    async with session_factory() as db:
        remaining = await db.execute(
            select(func.count()).select_from(Transcript).where(Transcript.call_id == call_id)
        )
    assert remaining.scalar_one() == 0


@pytest.mark.asyncio
async def test_purge_scrubs_dynamic_vars_on_terminal_calls(session_factory):
    call_id = await _seed_call(
        session_factory, status=CallStatus.COMPLETED, dynamic_vars={"elder_name": "Ada"}
    )
    async with session_factory() as db:
        _, scrubbed = await retention.purge_expired(db, days=30, now=_FUTURE)
        await db.commit()
    assert scrubbed == 1
    async with session_factory() as db:
        row = await db.get(Call, call_id)
    assert row.dynamic_vars == {}


@pytest.mark.asyncio
async def test_purge_nulls_recording_uri_on_terminal_calls(session_factory):
    # A terminal call whose only remaining PHI is the recording URI must still be
    # scrubbed, so GET /calls/{id} can no longer mint a signed URL for the audio.
    call_id = await _seed_call(session_factory, status=CallStatus.COMPLETED, dynamic_vars={})
    async with session_factory() as db:
        row = await db.get(Call, call_id)
        row.recording_uri = "gs://bkt/recordings/2099-01-01/x.ogg"
        await db.commit()
    async with session_factory() as db:
        _, scrubbed = await retention.purge_expired(db, days=30, now=_FUTURE)
        await db.commit()
    assert scrubbed == 1
    async with session_factory() as db:
        row = await db.get(Call, call_id)
    assert row.recording_uri is None


@pytest.mark.asyncio
async def test_purge_leaves_non_terminal_calls_untouched(session_factory):
    call_id = await _seed_call(
        session_factory, status=CallStatus.IN_PROGRESS, dynamic_vars={"elder_name": "Ada"}
    )
    async with session_factory() as db:
        _, scrubbed = await retention.purge_expired(db, days=30, now=_FUTURE)
        await db.commit()
    assert scrubbed == 0
    async with session_factory() as db:
        row = await db.get(Call, call_id)
    assert row.dynamic_vars == {"elder_name": "Ada"}


@pytest.mark.asyncio
async def test_purge_keeps_recent_rows(session_factory):
    # With a present-day clock, just-created rows are inside the window and survive.
    call_id = await _seed_call(
        session_factory, status=CallStatus.COMPLETED, dynamic_vars={"elder_name": "Ada"}
    )
    await _add_transcript(session_factory, call_id)
    recent = datetime.now(UTC) + timedelta(seconds=5)
    async with session_factory() as db:
        transcripts, scrubbed = await retention.purge_expired(db, days=30, now=recent)
        await db.commit()
    assert transcripts == 0
    assert scrubbed == 0


@pytest.mark.asyncio
async def test_run_poller_noop_when_retention_unset():
    import asyncio

    from usan_api.settings import Settings

    settings = Settings(
        DATABASE_URL="postgresql://u:p@host/db",
        LIVEKIT_API_KEY="key",
        LIVEKIT_API_SECRET="a" * 32,
        LIVEKIT_URL="ws://livekit:7880",
        JWT_SIGNING_KEY="s" * 32,
        OPERATOR_API_KEY="o" * 32,
    )
    assert settings.phi_retention_days is None
    stop = asyncio.Event()
    stop.set()
    # Returns immediately without starting a loop because retention is unset.
    await retention.run_poller(settings, stop)
