import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories import callback_requests as cb_repo


async def _seed_call_and_elder(db) -> tuple[uuid.UUID, uuid.UUID]:
    elder_id = uuid.uuid4()
    call_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO elders (id, name, phone_e164, timezone) "
            "VALUES (CAST(:id AS uuid), 'Ada', :p, 'UTC')"
        ),
        {"id": str(elder_id), "p": f"+1555{str(elder_id.int)[:7]}"},
    )
    await db.execute(
        text(
            "INSERT INTO calls (id, elder_id, direction, status) "
            "VALUES (CAST(:cid AS uuid), CAST(:eid AS uuid), 'outbound', 'in_progress')"
        ),
        {"cid": str(call_id), "eid": str(elder_id)},
    )
    return call_id, elder_id


@pytest.fixture
async def db(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_create_callback_request_persists_row(db):
    call_id, elder_id = await _seed_call_and_elder(db)
    when = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
    row = await cb_repo.create_callback_request(
        db,
        call_id=call_id,
        elder_id=elder_id,
        requested_time_text="tomorrow afternoon",
        requested_at=when,
        notes="prefers a call back",
    )
    await db.commit()
    assert isinstance(row.id, int)
    assert row.call_id == call_id
    assert row.elder_id == elder_id
    assert row.requested_time_text == "tomorrow afternoon"
    assert row.requested_at == when
    assert row.notes == "prefers a call back"
    assert row.status == "open"


async def test_create_callback_request_allows_null_requested_at(db):
    call_id, elder_id = await _seed_call_and_elder(db)
    row = await cb_repo.create_callback_request(
        db,
        call_id=call_id,
        elder_id=elder_id,
        requested_time_text="sometime soon",
        requested_at=None,
        notes=None,
    )
    await db.commit()
    assert row.requested_at is None
    assert row.notes is None


async def test_list_callback_requests_filters_by_status(db):
    call_id, elder_id = await _seed_call_and_elder(db)
    await cb_repo.create_callback_request(
        db,
        call_id=call_id,
        elder_id=elder_id,
        requested_time_text="first",
        requested_at=None,
        notes=None,
    )
    await db.commit()
    rows = await cb_repo.list_callback_requests(db, status="open", limit=50)
    assert any(r.requested_time_text == "first" for r in rows)
    none_rows = await cb_repo.list_callback_requests(db, status="resolved", limit=50)
    assert all(r.status == "resolved" for r in none_rows)
