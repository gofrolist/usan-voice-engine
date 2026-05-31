import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.repositories import elders as elders_repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_parent(factory) -> uuid.UUID:
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
        parent = Call(
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.NO_ANSWER,
            attempt=1,
        )
        db.add(parent)
        await db.flush()
        await db.commit()
        return parent.id


@pytest.mark.asyncio
async def test_at_most_one_retry_child_per_parent(session_factory):
    parent_id = await _seed_parent(session_factory)

    async with session_factory() as db:
        db.add(
            Call(
                direction=CallDirection.OUTBOUND,
                status=CallStatus.QUEUED,
                parent_call_id=parent_id,
                attempt=2,
            )
        )
        await db.commit()

    async with session_factory() as db:
        db.add(
            Call(
                direction=CallDirection.OUTBOUND,
                status=CallStatus.QUEUED,
                parent_call_id=parent_id,
                attempt=2,
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.asyncio
async def test_null_parent_call_id_not_constrained(session_factory):
    # The unique index is partial (WHERE parent_call_id IS NOT NULL): many rows
    # may have a NULL parent (every initial call), so this must not raise.
    async with session_factory() as db:
        for _ in range(3):
            phone = f"+1555{str(uuid.uuid4().int)[:7]}"
            elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
            db.add(
                Call(
                    elder_id=elder.id,
                    direction=CallDirection.OUTBOUND,
                    status=CallStatus.QUEUED,
                )
            )
        await db.commit()  # no IntegrityError
