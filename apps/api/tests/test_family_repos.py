"""family_contacts + family_tasks repositories (US2 / T026).

Covers contact CRUD + phone lookup + alert-recipient filtering, and the family-task
state machine (open → delivered → closed; needs_safety_review excluded from injection).
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api.repositories import family_contacts as contacts_repo
from usan_api.repositories import family_tasks as tasks_repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    async with session_factory() as db:
        await db.execute(
            text("TRUNCATE family_tasks, family_contacts, calls, contacts RESTART IDENTITY CASCADE")
        )
        await db.commit()


async def _make_contact(db, *, name="Ada") -> uuid.UUID:
    row = await db.execute(
        text(
            "INSERT INTO contacts (name, phone_e164, timezone) VALUES (:n, :p, 'UTC') RETURNING id"
        ),
        {"n": name, "p": f"+1555{str(uuid.uuid4().int)[:7]}"},
    )
    return row.scalar_one()


async def _make_call(db, contact_id: uuid.UUID) -> uuid.UUID:
    row = await db.execute(
        text(
            "INSERT INTO calls (contact_id, direction, status) "
            "VALUES (:e, 'outbound', 'completed') RETURNING id"
        ),
        {"e": contact_id},
    )
    return row.scalar_one()


async def test_family_contact_crud_and_phone_lookup(session_factory) -> None:
    async with session_factory() as db:
        contact_id = await _make_contact(db)
        contact = await contacts_repo.create_family_contact(
            db,
            contact_id=contact_id,
            name="Dana",
            phone_e164="+15550000001",
            relationship="daughter",
            alert_prefs={"crisis": True, "missed": False},
        )
        await db.commit()
        assert contact.id is not None
        assert contact.alert_prefs == {"crisis": True, "missed": False}

    async with session_factory() as db:
        listed = await contacts_repo.list_family_contacts(db, contact_id=contact_id)
        assert [c.name for c in listed] == ["Dana"]

        by_phone = await contacts_repo.find_contacts_by_phone(db, "+15550000001")
        assert len(by_phone) == 1
        assert by_phone[0].contact_id == contact_id

        updated = await contacts_repo.update_family_contact(db, contact.id, relationship="neighbor")
        await db.commit()
        assert updated is not None
        assert updated.relationship == "neighbor"


async def test_alert_recipients_respects_prefs(session_factory) -> None:
    async with session_factory() as db:
        contact_id = await _make_contact(db)
        # Default prefs ({}) => opted in to all kinds (fail-open for life-safety alerts).
        await contacts_repo.create_family_contact(
            db, contact_id=contact_id, name="Default", phone_e164="+15550000010"
        )
        # Explicit opt-out of "missed_call" but opted in to "crisis".
        await contacts_repo.create_family_contact(
            db,
            contact_id=contact_id,
            name="Picky",
            phone_e164="+15550000011",
            alert_prefs={"missed_call": False, "crisis": True},
        )
        await db.commit()

    async with session_factory() as db:
        crisis = await contacts_repo.list_alert_recipients(db, contact_id=contact_id, kind="crisis")
        assert {c.name for c in crisis} == {"Default", "Picky"}
        missed = await contacts_repo.list_alert_recipients(
            db, contact_id=contact_id, kind="missed_call"
        )
        assert {c.name for c in missed} == {"Default"}  # Picky opted out of missed_call


async def test_family_task_lifecycle_and_open_filter(session_factory) -> None:
    async with session_factory() as db:
        contact_id = await _make_contact(db)
        call_id = await _make_call(db, contact_id)
        open_task = await tasks_repo.create_family_task(
            db, contact_id=contact_id, family_contact_id=None, message="remind mom to drink water"
        )
        # A task flagged for safety review must NOT be injected into the call prompt.
        await tasks_repo.create_family_task(
            db,
            contact_id=contact_id,
            family_contact_id=None,
            message="skip her heart pills today",
            needs_safety_review=True,
        )
        await db.commit()

    async with session_factory() as db:
        injectable = await tasks_repo.list_open_family_tasks(db, contact_id=contact_id)
        assert [t.message for t in injectable] == ["remind mom to drink water"]

        delivered = await tasks_repo.mark_delivered(db, open_task.id, call_id=call_id)
        await db.commit()
        assert delivered is not None
        assert delivered.status == "delivered"
        assert delivered.delivered_call_id == call_id

    async with session_factory() as db:
        # Delivered tasks are no longer injected (not repeated next call).
        assert await tasks_repo.list_open_family_tasks(db, contact_id=contact_id) == []
        closed = await tasks_repo.close_family_task(db, open_task.id, actor="agent")
        await db.commit()
        assert closed is not None
        assert closed.status == "closed"
