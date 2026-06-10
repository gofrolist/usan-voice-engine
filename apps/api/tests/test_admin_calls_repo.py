"""admin_calls repository: the admin-plane filtered/paged calls read model (spec §4.1)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.repositories import admin_calls as admin_calls_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    async with session_factory() as db:
        await db.execute(text("TRUNCATE calls, elders CASCADE"))
        await db.commit()


async def _seed_elder(factory, *, name: str = "Calls Elder") -> uuid.UUID:
    phone = f"+1555{str(uuid.uuid4().int)[:7].zfill(7)}"
    async with factory() as db:
        elder = await elders_repo.create_elder(
            db, name=name, phone_e164=phone, timezone="America/New_York"
        )
        await db.commit()
        return elder.id


async def _create_call(
    factory,
    elder_id: uuid.UUID,
    *,
    direction: CallDirection = CallDirection.OUTBOUND,
    status: CallStatus = CallStatus.QUEUED,
    idempotency_key: str | None = None,
) -> uuid.UUID:
    async with factory() as db:
        call = await calls_repo.create_call(
            db,
            elder_id=elder_id,
            direction=direction,
            status=status,
            idempotency_key=idempotency_key,
        )
        await db.commit()
        return call.id


async def test_orders_newest_first_id_tiebreaker(session_factory):
    elder_id = await _seed_elder(session_factory)

    # Three calls in ONE flush share created_at (func.now() is the txn
    # timestamp), so ordering must fall back to the id DESC tiebreaker.
    async with session_factory() as db:
        tied = [
            (
                await calls_repo.create_call(
                    db,
                    elder_id=elder_id,
                    direction=CallDirection.OUTBOUND,
                    status=CallStatus.QUEUED,
                )
            ).id
            for _ in range(3)
        ]
        await db.commit()

    # A fourth call in a later txn has a strictly later created_at: sorts first.
    newest = await _create_call(session_factory, elder_id)

    # Python UUID ordering matches Postgres uuid byte ordering, so id DESC is
    # computable client-side.
    expected = [newest, *sorted(tied, reverse=True)]
    async with session_factory() as db:
        rows = await admin_calls_repo.list_calls(db)
        assert [call.id for call, _, _ in rows] == expected


async def test_origin_filter_matrix(session_factory):
    elder_id = await _seed_elder(session_factory)
    sched_id = await _create_call(
        session_factory, elder_id, idempotency_key=f"sched:{elder_id}:2026-06-10"
    )
    batch_id = await _create_call(
        session_factory, elder_id, idempotency_key=f"batch:{uuid.uuid4()}:0"
    )
    operator_id = await _create_call(session_factory, elder_id, idempotency_key="operator-key-1")
    # NULL-key outbound: the retry-child shape — matches adhoc (chain root carries
    # provenance; documented spec §4.1 caveat).
    null_key_id = await _create_call(session_factory, elder_id, idempotency_key=None)
    # Inbound calls are always NULL-key; the direction guard keeps them out of adhoc.
    async with session_factory() as db:
        inbound = await calls_repo.create_inbound_call(
            db, elder_id=elder_id, livekit_room="room-inbound-origin"
        )
        await db.commit()
        inbound_id = inbound.id

    async with session_factory() as db:
        sched_rows = await admin_calls_repo.list_calls(db, origin="schedule")
        assert [call.id for call, _, _ in sched_rows] == [sched_id]

        batch_rows = await admin_calls_repo.list_calls(db, origin="batch")
        assert [call.id for call, _, _ in batch_rows] == [batch_id]

        adhoc_rows = await admin_calls_repo.list_calls(db, origin="adhoc")
        assert {call.id for call, _, _ in adhoc_rows} == {operator_id, null_key_id}

        all_rows = await admin_calls_repo.list_calls(db, origin=None)
        assert {call.id for call, _, _ in all_rows} == {
            sched_id,
            batch_id,
            operator_id,
            null_key_id,
            inbound_id,
        }


async def test_status_direction_elder_filters(session_factory):
    e1 = await _seed_elder(session_factory)
    e2 = await _seed_elder(session_factory)
    completed = await _create_call(session_factory, e1, status=CallStatus.COMPLETED)
    queued = await _create_call(session_factory, e1, status=CallStatus.QUEUED)
    inbound = await _create_call(
        session_factory, e2, direction=CallDirection.INBOUND, status=CallStatus.IN_PROGRESS
    )

    async with session_factory() as db:
        by_status = await admin_calls_repo.list_calls(db, status=CallStatus.COMPLETED)
        assert [call.id for call, _, _ in by_status] == [completed]

        by_direction = await admin_calls_repo.list_calls(db, direction=CallDirection.INBOUND)
        assert [call.id for call, _, _ in by_direction] == [inbound]

        by_elder = await admin_calls_repo.list_calls(db, elder_id=e1)
        assert {call.id for call, _, _ in by_elder} == {completed, queued}

        combined = await admin_calls_repo.list_calls(db, elder_id=e2, status=CallStatus.QUEUED)
        assert combined == []


async def test_created_range_to_exclusive(session_factory):
    elder_id = await _seed_elder(session_factory)
    early = await _create_call(session_factory, elder_id)
    mid = await _create_call(session_factory, elder_id)
    late = await _create_call(session_factory, elder_id)
    boundary = NOW + timedelta(minutes=1)
    async with session_factory() as db:
        for call_id, created_at in (
            (early, NOW),
            (mid, boundary),
            (late, NOW + timedelta(minutes=2)),
        ):
            await db.execute(update(Call).where(Call.id == call_id).values(created_at=created_at))
        await db.commit()

    async with session_factory() as db:
        # A row whose created_at EQUALS created_to is excluded (exclusive upper bound).
        upto = await admin_calls_repo.list_calls(db, created_to=boundary)
        assert [call.id for call, _, _ in upto] == [early]

        # created_from is inclusive: the boundary row is in.
        since = await admin_calls_repo.list_calls(db, created_from=boundary)
        assert {call.id for call, _, _ in since} == {mid, late}

        window = await admin_calls_repo.list_calls(
            db, created_from=NOW, created_to=NOW + timedelta(minutes=2)
        )
        assert {call.id for call, _, _ in window} == {early, mid}


async def test_limit_clamp_and_offset(session_factory):
    assert admin_calls_repo.MAX_ADMIN_CALLS_LIMIT == 500  # spec §4.1 bounded read

    elder_id = await _seed_elder(session_factory)
    ids = [await _create_call(session_factory, elder_id) for _ in range(3)]
    # Force identical created_at so ordering must fall back to the id tiebreaker.
    async with session_factory() as db:
        await db.execute(update(Call).where(Call.id.in_(ids)).values(created_at=NOW))
        await db.commit()

    expected = sorted(ids, reverse=True)
    async with session_factory() as db:
        # limit clamps low (0 -> 1) and high (10_000 -> MAX_ADMIN_CALLS_LIMIT,
        # silently — no raise); offset pages past rows in the same ordering.
        first_page = await admin_calls_repo.list_calls(db, limit=0)
        assert [call.id for call, _, _ in first_page] == expected[:1]

        rest = await admin_calls_repo.list_calls(db, limit=10_000, offset=1)
        assert [call.id for call, _, _ in rest] == expected[1:]


async def test_elder_join_and_deleted_elder(session_factory):
    phone = f"+1555{str(uuid.uuid4().int)[:7].zfill(7)}"
    async with session_factory() as db:
        elder = await elders_repo.create_elder(
            db, name="Join Elder", phone_e164=phone, timezone="America/New_York"
        )
        await db.commit()
        elder_id = elder.id
    call_id = await _create_call(session_factory, elder_id)

    async with session_factory() as db:
        rows = await admin_calls_repo.list_calls(db)
        assert [(call.id, name, phone_e164) for call, name, phone_e164 in rows] == [
            (call_id, "Join Elder", phone)
        ]

    # ON DELETE SET NULL: the call survives the elder's deletion with NULL joins.
    async with session_factory() as db:
        await db.execute(text("DELETE FROM elders"))
        await db.commit()

    async with session_factory() as db:
        rows = await admin_calls_repo.list_calls(db)
        assert len(rows) == 1
        call, name, phone_e164 = rows[0]
        assert call.id == call_id
        assert call.elder_id is None
        assert name is None
        assert phone_e164 is None
