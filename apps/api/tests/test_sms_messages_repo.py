import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import sms_messages as sms_repo


async def _seed_call(url):
    engine = create_async_engine(url, poolclass=NullPool)
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
            )
            await db.commit()
            return url, call.id, elder.id
    finally:
        await engine.dispose()


async def _run(url, call_id, elder_id):
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            row = await sms_repo.create_sms_message(
                db,
                call_id=call_id,
                elder_id=elder_id,
                to_number="+15557654321",
                template_key="t",
                body="hi",
            )
            await db.commit()
            assert row.status == "pending"
            pend = await sms_repo.get_pending_for_call(db, call_id)
            assert len(pend) == 1

            sent = await sms_repo.mark_sent(db, row.id, telnyx_message_id="msg-1")
            await db.commit()
            assert sent is not None
            assert sent.status == "sent"
            assert sent.telnyx_message_id == "msg-1"

            # Idempotent: second mark_sent on an already-sent row no-ops (returns None).
            again = await sms_repo.mark_sent(db, row.id, telnyx_message_id="msg-2")
            assert again is None
            # And mark_failed on a non-pending row also no-ops.
            failed = await sms_repo.mark_failed(db, row.id, error={"reason": "x"})
            assert failed is None
            assert await sms_repo.get_pending_for_call(db, call_id) == []
    finally:
        await engine.dispose()


def test_sms_repo_create_and_status_guarded_transitions(async_database_url):
    url, call_id, elder_id = asyncio.run(_seed_call(async_database_url))
    asyncio.run(_run(url, call_id, elder_id))


async def _run_failed(url, call_id, elder_id):
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            row = await sms_repo.create_sms_message(
                db,
                call_id=call_id,
                elder_id=elder_id,
                to_number="+1",
                template_key="t",
                body="hi",
            )
            await db.commit()
            failed = await sms_repo.mark_failed(db, row.id, error={"reason": "messaging_disabled"})
            await db.commit()
            assert failed is not None
            assert failed.status == "failed"
            assert failed.error == {"reason": "messaging_disabled"}
            msgs = await sms_repo.list_messages(db, status="failed")
            assert any(m.id == row.id for m in msgs)
    finally:
        await engine.dispose()


def test_sms_repo_mark_failed(async_database_url):
    url, call_id, elder_id = asyncio.run(_seed_call(async_database_url))
    asyncio.run(_run_failed(url, call_id, elder_id))
