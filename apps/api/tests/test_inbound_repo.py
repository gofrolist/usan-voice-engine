import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import wellness as wellness_repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _phone() -> str:
    return f"+1555{str(uuid.uuid4().int)[:7]}"


@pytest.mark.asyncio
async def test_get_contact_by_phone_found_and_missing(session_factory):
    phone = _phone()
    async with session_factory() as db:
        contact = await contacts_repo.create_contact(
            db, name="Ada", phone_e164=phone, timezone="UTC"
        )
        await db.commit()
        eid = contact.id
    async with session_factory() as db:
        found = await contacts_repo.get_contact_by_phone(db, phone)
        missing = await contacts_repo.get_contact_by_phone(db, "+10000000000")
    assert found is not None
    assert found.id == eid
    assert missing is None


@pytest.mark.asyncio
async def test_create_inbound_call_is_answered_in_progress(session_factory):
    phone = _phone()
    async with session_factory() as db:
        contact = await contacts_repo.create_contact(
            db, name="Ada", phone_e164=phone, timezone="UTC"
        )
        call = await calls_repo.create_inbound_call(
            db,
            contact_id=contact.id,
            livekit_room="usan-inbound-r1",
            sip_call_id="SIP-abc123",
            dynamic_vars={"contact_name": "Ada"},
        )
        await db.commit()
    assert call.direction is CallDirection.INBOUND
    assert call.status is CallStatus.IN_PROGRESS
    assert call.answered_at is not None
    assert call.started_at is not None
    assert call.livekit_room == "usan-inbound-r1"
    assert call.sip_call_id == "SIP-abc123"
    assert call.dynamic_vars == {"contact_name": "Ada"}


@pytest.mark.asyncio
async def test_create_inbound_call_allows_null_contact(session_factory):
    async with session_factory() as db:
        call = await calls_repo.create_inbound_call(
            db, contact_id=None, livekit_room="usan-inbound-r2"
        )
        await db.commit()
    assert call.contact_id is None
    assert call.direction is CallDirection.INBOUND
    assert call.dynamic_vars == {}


@pytest.mark.asyncio
async def test_get_latest_for_contact_returns_most_recent(session_factory):
    phone = _phone()
    async with session_factory() as db:
        contact = await contacts_repo.create_contact(
            db, name="Ada", phone_e164=phone, timezone="UTC"
        )
        call = await calls_repo.create_inbound_call(
            db, contact_id=contact.id, livekit_room="usan-inbound-r3"
        )
        await db.commit()
        assert await wellness_repo.get_latest_for_contact(db, contact.id) is None
        await wellness_repo.create_wellness_log(
            db, call_id=call.id, contact_id=contact.id, mood=3, pain_level=2, notes="ok"
        )
        await wellness_repo.create_wellness_log(
            db, call_id=call.id, contact_id=contact.id, mood=5, pain_level=0, notes="great"
        )
        await db.commit()
        latest = await wellness_repo.get_latest_for_contact(db, contact.id)
    assert latest is not None
    assert latest.mood == 5  # tie-broken by id desc (both share the txn's now())
