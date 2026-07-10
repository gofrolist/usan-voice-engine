"""T004 (US-foundational): PHI-minimized family/operator notification builder.

The builder creates `sms_messages` rows with call_id IS NULL, a `kind`, and an
optional `dedupe_key`. Bodies are built from fixed, PHI-FREE templates: a family
alert says "please check in", never a mood score / medication name / transcript.
"""

import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import notifications
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import sms_messages as sms_repo

# Clinical / PHI markers that must NEVER appear in a family-facing SMS body.
_CLINICAL_TERMS = (
    "mood",
    "pain",
    "medication",
    "suicid",
    "overdose",
    "depress",
    "transcript",
    "diagnos",
)

_FAMILY_NUMBER = "+15557654321"


def _engine(url: str):
    return create_async_engine(url, poolclass=NullPool)


async def _seed_contact(url: str) -> uuid.UUID:
    engine = _engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    try:
        async with factory() as db:
            contact = await contacts_repo.create_contact(
                db, name="Ada", phone_e164=phone, timezone="UTC"
            )
            await db.commit()
            return contact.id
    finally:
        await engine.dispose()


def test_family_alert_bodies_are_phi_minimized():
    for reason in ("crisis", "missed_call"):
        body = notifications.build_family_alert_body(reason)
        assert body
        assert isinstance(body, str)
        low = body.lower()
        for term in _CLINICAL_TERMS:
            assert term not in low, f"family alert ({reason}) leaks clinical term: {term}"


def test_opt_out_ack_body_is_phi_minimized():
    body = notifications.build_opt_out_ack_body()
    assert body
    assert isinstance(body, str)
    low = body.lower()
    for term in _CLINICAL_TERMS:
        assert term not in low
    # An opt-out ack should reference stopping/unsubscribing, never clinical detail.
    assert any(w in low for w in ("unsubscrib", "stop", "no longer"))


def test_enqueue_family_alert_creates_pending_notification_row(client, async_database_url):
    contact_id = asyncio.run(_seed_contact(async_database_url))

    async def _do():
        engine = _engine(async_database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                row = await notifications.enqueue_family_alert(
                    db,
                    contact_id=contact_id,
                    to_number=_FAMILY_NUMBER,
                    reason="crisis",
                    dedupe_key=f"crisis:{uuid.uuid4()}",
                )
                await db.commit()
                return row
        finally:
            await engine.dispose()

    row = asyncio.run(_do())
    assert row is not None
    assert row.kind == "family_alert"
    assert row.call_id is None  # a non-call notification
    assert row.template_key is None  # system template, not a per-profile key
    assert row.status == "pending"
    assert row.to_number == _FAMILY_NUMBER


def test_enqueue_family_alert_is_idempotent_on_dedupe_key(client, async_database_url):
    contact_id = asyncio.run(_seed_contact(async_database_url))
    dedupe = f"crisis:{uuid.uuid4()}"

    async def _do():
        engine = _engine(async_database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                first = await notifications.enqueue_family_alert(
                    db,
                    contact_id=contact_id,
                    to_number=_FAMILY_NUMBER,
                    reason="crisis",
                    dedupe_key=dedupe,
                )
                await db.commit()
                second = await notifications.enqueue_family_alert(
                    db,
                    contact_id=contact_id,
                    to_number=_FAMILY_NUMBER,
                    reason="crisis",
                    dedupe_key=dedupe,
                )
                await db.commit()
                rows = await sms_repo.list_messages(db, limit=200)
                matched = [r for r in rows if r.dedupe_key == dedupe]
                return first, second, matched
        finally:
            await engine.dispose()

    first, second, matched = asyncio.run(_do())
    assert first is not None
    assert second is not None
    assert first.id == second.id  # the second enqueue returned the existing row
    assert len(matched) == 1  # exactly one row exists for the dedupe key


def test_enqueue_opt_out_ack_creates_row(client, async_database_url):
    contact_id = asyncio.run(_seed_contact(async_database_url))

    async def _do():
        engine = _engine(async_database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                row = await notifications.enqueue_opt_out_ack(
                    db,
                    contact_id=contact_id,
                    to_number=_FAMILY_NUMBER,
                    dedupe_key=f"optout:{uuid.uuid4()}",
                )
                await db.commit()
                return row
        finally:
            await engine.dispose()

    row = asyncio.run(_do())
    assert row is not None
    assert row.kind == "opt_out_ack"
    assert row.call_id is None
