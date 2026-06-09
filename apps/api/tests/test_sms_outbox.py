import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import sms_outbox, telnyx_messaging
from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import sms_messages as sms_repo


def _patch_factory(monkeypatch, url):
    # flush_pending_sms() opens its own session via the module-global cached engine,
    # which uses a pooled (non-NullPool) connection. The test drives it across several
    # independent asyncio.run() loops, and a pooled connection bound to a closed loop
    # cannot be reused on the next run. Point flush at a NullPool factory at its import
    # site (the same pattern conftest's `client` fixture and test_retry_orchestrator use)
    # so every run gets a fresh connection.
    engine = create_async_engine(url, poolclass=NullPool)
    monkeypatch.setattr(
        sms_outbox,
        "get_session_factory",
        lambda: async_sessionmaker(engine, expire_on_commit=False),
    )


async def _seed_pending(url):
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
            await sms_repo.create_sms_message(
                db,
                call_id=call.id,
                elder_id=elder.id,
                to_number=phone,
                template_key="t",
                body="hi",
            )
            await db.commit()
            return call.id
    finally:
        await engine.dispose()


async def _status_of(url, call_id):
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            rows = await sms_repo.list_messages(db, limit=100)
            return [r for r in rows if r.call_id == call_id]
    finally:
        await engine.dispose()


def test_flush_marks_failed_when_messaging_disabled(client, async_database_url, monkeypatch):
    # client fixture sets env; messaging is disabled by default (no TELNYX_MESSAGING_ENABLED).
    _patch_factory(monkeypatch, async_database_url)
    call_id = asyncio.run(_seed_pending(async_database_url))
    asyncio.run(sms_outbox.flush_pending_sms(call_id))
    rows = asyncio.run(_status_of(async_database_url, call_id))
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error == {"reason": "messaging_disabled"}


def test_flush_sends_and_marks_sent(client, async_database_url, monkeypatch):
    monkeypatch.setenv("TELNYX_MESSAGING_ENABLED", "true")
    monkeypatch.setenv("TELNYX_MESSAGING_API_KEY", "KEY")
    monkeypatch.setenv("TELNYX_MESSAGING_PROFILE_ID", "mp1")
    monkeypatch.setenv("TELNYX_FROM_NUMBER", "+15551230000")
    from usan_api.settings import get_settings

    get_settings.cache_clear()

    async def _fake_send(settings, *, to_number, body):
        return "msg-xyz"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _fake_send)
    _patch_factory(monkeypatch, async_database_url)

    call_id = asyncio.run(_seed_pending(async_database_url))
    asyncio.run(sms_outbox.flush_pending_sms(call_id))
    rows = asyncio.run(_status_of(async_database_url, call_id))
    assert rows[0].status == "sent"
    assert rows[0].telnyx_message_id == "msg-xyz"

    # Idempotent: a second flush re-sends nothing (row no longer pending).
    asyncio.run(sms_outbox.flush_pending_sms(call_id))
    rows2 = asyncio.run(_status_of(async_database_url, call_id))
    assert rows2[0].status == "sent"
    get_settings.cache_clear()


def test_flush_marks_failed_on_send_error(client, async_database_url, monkeypatch):
    monkeypatch.setenv("TELNYX_MESSAGING_ENABLED", "true")
    monkeypatch.setenv("TELNYX_MESSAGING_API_KEY", "KEY")
    monkeypatch.setenv("TELNYX_MESSAGING_PROFILE_ID", "mp1")
    monkeypatch.setenv("TELNYX_FROM_NUMBER", "+15551230000")
    from usan_api.settings import get_settings

    get_settings.cache_clear()

    async def _boom(settings, *, to_number, body):
        raise telnyx_messaging.TelnyxMessagingError("nope")

    monkeypatch.setattr(telnyx_messaging, "send_sms", _boom)
    _patch_factory(monkeypatch, async_database_url)
    call_id = asyncio.run(_seed_pending(async_database_url))
    asyncio.run(sms_outbox.flush_pending_sms(call_id))
    rows = asyncio.run(_status_of(async_database_url, call_id))
    assert rows[0].status == "failed"
    assert rows[0].error["reason"] == "send_failed"
    get_settings.cache_clear()
