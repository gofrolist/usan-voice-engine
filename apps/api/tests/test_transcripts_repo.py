import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Transcript
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import transcripts as transcripts_repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_call(factory) -> uuid.UUID:
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.IN_PROGRESS,
            livekit_room="usan-outbound-tr",
        )
        await db.commit()
        return call.id


@pytest.mark.asyncio
async def test_create_transcript_segments_bulk_inserts(session_factory):
    call_id = await _seed_call(session_factory)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    segments = [
        {"role": "assistant", "content": "Hello!", "started_at": now},
        {"role": "user", "content": "I'm good", "started_at": now},
        {
            "role": "tool",
            "content": "log_wellness",
            "tool_name": "log_wellness",
            "tool_args": {"mood": 4},
            "started_at": now,
        },
    ]
    async with session_factory() as db:
        count = await transcripts_repo.create_transcript_segments(
            db, call_id=call_id, segments=segments
        )
        await db.commit()
    assert count == 3
    async with session_factory() as db:
        total = await db.execute(
            select(func.count()).select_from(Transcript).where(Transcript.call_id == call_id)
        )
        rows = await db.execute(
            select(Transcript).where(Transcript.call_id == call_id).order_by(Transcript.id)
        )
    assert total.scalar_one() == 3
    tool_row = [r for r in rows.scalars().all() if r.role == "tool"][0]
    assert tool_row.tool_name == "log_wellness"
    assert tool_row.tool_args == {"mood": 4}
