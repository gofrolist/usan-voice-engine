"""T088 (US2): notification recipient resolution + operator fallback (FR-013).

dispatch_family_alert resolves opted-in family contacts and reports whether ANY contact
is registered (so callers can route to the operator queue only on a true absence, not on
an opt-out). ensure_operator_missed_flag is the idempotent operator-queue entry.
"""

import json
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api import notifications
from usan_api.db.models import FollowUpFlag, SmsMessage
from usan_api.repositories import follow_up_flags as flags_repo

_FAMILY = "+15557654321"


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    async with session_factory() as db:
        await db.execute(
            text(
                "TRUNCATE family_tasks, family_contacts, follow_up_flags, sms_messages, "
                "calls, contacts RESTART IDENTITY CASCADE"
            )
        )
        await db.commit()


async def _contact(db) -> uuid.UUID:
    return (
        await db.execute(
            text(
                "INSERT INTO contacts (name, phone_e164, timezone) "
                "VALUES ('Ada', :p, 'UTC') RETURNING id"
            ),
            {"p": f"+1555{str(uuid.uuid4().int)[:7]}"},
        )
    ).scalar_one()


async def _family_contact(db, contact_id, *, prefs: dict | None = None) -> None:
    await db.execute(
        text(
            "INSERT INTO family_contacts (contact_id, name, phone_e164, alert_prefs) "
            "VALUES (:e, 'Dana', :p, CAST(:pr AS JSONB))"
        ),
        {"e": contact_id, "p": _FAMILY, "pr": json.dumps(prefs or {})},
    )


async def _call(db, contact_id) -> uuid.UUID:
    return (
        await db.execute(
            text(
                "INSERT INTO calls (contact_id, direction, status) "
                "VALUES (:e, 'outbound', 'no_answer') RETURNING id"
            ),
            {"e": contact_id},
        )
    ).scalar_one()


async def test_dispatch_no_contacts_returns_empty_and_sends_nothing(session_factory):
    async with session_factory() as db:
        contact_id = await _contact(db)
        await db.commit()
    async with session_factory() as db:
        dispatch = await notifications.dispatch_family_alert(
            db, contact_id=contact_id, reason="crisis", dedupe_base="crisis:1"
        )
        await db.commit()
        assert dispatch.notified == []
        assert dispatch.had_contacts is False
    async with session_factory() as db:
        sent = (await db.execute(select(SmsMessage))).scalars().all()
        assert sent == []


async def test_dispatch_opted_in_contact_enqueues(session_factory):
    async with session_factory() as db:
        contact_id = await _contact(db)
        await _family_contact(db, contact_id)
        await db.commit()
    async with session_factory() as db:
        dispatch = await notifications.dispatch_family_alert(
            db, contact_id=contact_id, reason="crisis", dedupe_base="crisis:7"
        )
        await db.commit()
        assert [c.phone_e164 for c in dispatch.notified] == [_FAMILY]
        assert dispatch.had_contacts is True
    async with session_factory() as db:
        sent = (await db.execute(select(SmsMessage))).scalars().all()
        assert len(sent) == 1
        assert sent[0].kind == "family_alert"
        assert sent[0].dedupe_key == f"crisis:7:{_FAMILY}"


async def test_dispatch_opted_out_contact_not_notified_but_had_contacts(session_factory):
    async with session_factory() as db:
        contact_id = await _contact(db)
        await _family_contact(db, contact_id, prefs={"crisis": False})
        await db.commit()
    async with session_factory() as db:
        dispatch = await notifications.dispatch_family_alert(
            db, contact_id=contact_id, reason="crisis", dedupe_base="crisis:9"
        )
        await db.commit()
        assert dispatch.notified == []
        assert dispatch.had_contacts is True  # a contact exists; they opted out
    async with session_factory() as db:
        assert (await db.execute(select(SmsMessage))).scalars().all() == []


async def test_ensure_operator_missed_flag_is_idempotent(session_factory):
    async with session_factory() as db:
        contact_id = await _contact(db)
        call_id = await _call(db, contact_id)
        await db.commit()
    async with session_factory() as db:
        await flags_repo.ensure_operator_missed_flag(db, call_id=call_id, contact_id=contact_id)
        await flags_repo.ensure_operator_missed_flag(db, call_id=call_id, contact_id=contact_id)
        await db.commit()
    async with session_factory() as db:
        flags = (
            (
                await db.execute(
                    select(FollowUpFlag).where(FollowUpFlag.category == "operator_alert")
                )
            )
            .scalars()
            .all()
        )
        assert len(flags) == 1
        assert flags[0].severity == "routine"
        assert flags[0].call_id == call_id
