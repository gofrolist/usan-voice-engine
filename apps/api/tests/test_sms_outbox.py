import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import counter_value as _counter_value
from usan_api import sms_outbox, telnyx_messaging
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import SmsMessage
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import sms_messages as sms_repo


def _patch_factory(request: pytest.FixtureRequest, monkeypatch, url: str) -> None:
    # flush_pending_sms() opens its own session via the module-global cached engine,
    # which uses a pooled (non-NullPool) connection. The test drives it across several
    # independent asyncio.run() loops, and a pooled connection bound to a closed loop
    # cannot be reused on the next run. Point flush at a NullPool factory at its import
    # site (the same pattern conftest's `client` fixture and test_retry_orchestrator use)
    # so every run gets a fresh connection.
    engine = create_async_engine(url, poolclass=NullPool)
    # Dispose at test teardown so the pool is released (no ResourceWarning under
    # strict warning filters). dispose() is async, so run it in its own loop.
    request.addfinalizer(lambda: asyncio.run(engine.dispose()))
    monkeypatch.setattr(
        sms_outbox,
        "get_session_factory",
        lambda: async_sessionmaker(engine, expire_on_commit=False),
    )


async def _seed_pending(url: str, count: int = 1) -> uuid.UUID:
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    try:
        async with factory() as db:
            contact = await contacts_repo.create_contact(
                db, name="A", phone_e164=phone, timezone="UTC"
            )
            call = await calls_repo.create_call(
                db,
                contact_id=contact.id,
                direction=CallDirection.OUTBOUND,
                status=CallStatus.IN_PROGRESS,
            )
            for i in range(count):
                await sms_repo.create_sms_message(
                    db,
                    call_id=call.id,
                    contact_id=contact.id,
                    to_number=phone,
                    template_key=f"t{i}",
                    body="hi",
                )
            await db.commit()
            return call.id
    finally:
        await engine.dispose()


async def _status_of(url: str, call_id: uuid.UUID) -> list[SmsMessage]:
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            rows = await sms_repo.list_messages(db, limit=100)
            return [r for r in rows if r.call_id == call_id]
    finally:
        await engine.dispose()


def test_flush_marks_failed_when_messaging_disabled(
    client, async_database_url, monkeypatch, request
):
    # client fixture sets env; messaging is disabled by default (no TELNYX_MESSAGING_ENABLED).
    # Clear any Settings cached by a sibling test that monkeypatched
    # TELNYX_MESSAGING_ENABLED=true, so this assertion can't depend on test ordering.
    from usan_api.settings import get_settings

    get_settings.cache_clear()
    _patch_factory(request, monkeypatch, async_database_url)
    call_id = asyncio.run(_seed_pending(async_database_url))
    asyncio.run(sms_outbox.flush_pending_sms(call_id))
    rows = asyncio.run(_status_of(async_database_url, call_id))
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error == {"reason": "messaging_disabled"}
    get_settings.cache_clear()


def test_flush_sends_and_marks_sent(client, async_database_url, monkeypatch, request):
    monkeypatch.setenv("TELNYX_MESSAGING_ENABLED", "true")
    monkeypatch.setenv("TELNYX_MESSAGING_API_KEY", "KEY")
    monkeypatch.setenv("TELNYX_MESSAGING_PROFILE_ID", "mp1")
    monkeypatch.setenv("TELNYX_FROM_NUMBER", "+15551230000")
    from usan_api.settings import get_settings

    get_settings.cache_clear()

    async def _fake_send(settings, *, to_number, body):
        return "msg-xyz"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _fake_send)
    _patch_factory(request, monkeypatch, async_database_url)

    from usan_api.observability.custom_metrics import SMS_MESSAGES_TOTAL

    before = _counter_value(SMS_MESSAGES_TOTAL, status="sent")
    call_id = asyncio.run(_seed_pending(async_database_url))
    asyncio.run(sms_outbox.flush_pending_sms(call_id))
    rows = asyncio.run(_status_of(async_database_url, call_id))
    assert rows[0].status == "sent"
    assert rows[0].telnyx_message_id == "msg-xyz"
    # The counter is the sole observability signal for SMS delivery.
    assert _counter_value(SMS_MESSAGES_TOTAL, status="sent") == before + 1

    # Idempotent: a second flush re-sends nothing (row no longer pending) and must NOT
    # double-count the metric (the end_call + room_finished completion race).
    asyncio.run(sms_outbox.flush_pending_sms(call_id))
    rows2 = asyncio.run(_status_of(async_database_url, call_id))
    assert rows2[0].status == "sent"
    assert _counter_value(SMS_MESSAGES_TOTAL, status="sent") == before + 1
    get_settings.cache_clear()


def test_flush_marks_failed_on_send_error(client, async_database_url, monkeypatch, request):
    monkeypatch.setenv("TELNYX_MESSAGING_ENABLED", "true")
    monkeypatch.setenv("TELNYX_MESSAGING_API_KEY", "KEY")
    monkeypatch.setenv("TELNYX_MESSAGING_PROFILE_ID", "mp1")
    monkeypatch.setenv("TELNYX_FROM_NUMBER", "+15551230000")
    from usan_api.settings import get_settings

    get_settings.cache_clear()

    async def _boom(settings, *, to_number, body):
        raise telnyx_messaging.TelnyxMessagingError("nope")

    monkeypatch.setattr(telnyx_messaging, "send_sms", _boom)
    _patch_factory(request, monkeypatch, async_database_url)
    call_id = asyncio.run(_seed_pending(async_database_url))
    asyncio.run(sms_outbox.flush_pending_sms(call_id))
    rows = asyncio.run(_status_of(async_database_url, call_id))
    assert rows[0].status == "failed"
    assert rows[0].error["reason"] == "send_failed"
    get_settings.cache_clear()


def _patch_factory_failing_commit(
    request: pytest.FixtureRequest, monkeypatch, url: str, *, fail_on_call: int
) -> None:
    """Like _patch_factory, but the Nth session.commit() raises (transient DB error)."""
    engine = create_async_engine(url, poolclass=NullPool)
    request.addfinalizer(lambda: asyncio.run(engine.dispose()))
    real_factory = async_sessionmaker(engine, expire_on_commit=False)
    calls = {"n": 0}

    def factory():
        session = real_factory()
        real_commit = session.commit

        async def commit():
            calls["n"] += 1
            if calls["n"] == fail_on_call:
                raise RuntimeError("simulated commit failure")
            await real_commit()

        session.commit = commit  # type: ignore[method-assign]
        return session

    monkeypatch.setattr(sms_outbox, "get_session_factory", lambda: factory)


def test_flush_commits_per_row_so_partial_progress_survives(
    client, async_database_url, monkeypatch, request
):
    # Two pending rows; the commit after the SECOND row fails. Per-row commits mean
    # row 1's `sent` status must survive — a single end-of-loop commit would roll
    # back BOTH rows after their Telnyx sends had already been dispatched, and the
    # next flush would re-send every message (review HIGH: duplicate SMS delivery).
    # The duplicate window must be at most the one row whose commit failed.
    monkeypatch.setenv("TELNYX_MESSAGING_ENABLED", "true")
    monkeypatch.setenv("TELNYX_MESSAGING_API_KEY", "KEY")
    monkeypatch.setenv("TELNYX_MESSAGING_PROFILE_ID", "mp1")
    monkeypatch.setenv("TELNYX_FROM_NUMBER", "+15551230000")
    from usan_api.settings import get_settings

    get_settings.cache_clear()

    async def _fake_send(settings, *, to_number, body):
        return f"msg-{uuid.uuid4().hex[:6]}"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _fake_send)
    _patch_factory_failing_commit(request, monkeypatch, async_database_url, fail_on_call=2)

    from usan_api.observability.custom_metrics import SMS_MESSAGES_TOTAL

    before = _counter_value(SMS_MESSAGES_TOTAL, status="sent")
    call_id = asyncio.run(_seed_pending(async_database_url, count=2))
    # Must not raise: flush runs as a fire-and-forget background task.
    asyncio.run(sms_outbox.flush_pending_sms(call_id))
    rows = asyncio.run(_status_of(async_database_url, call_id))
    statuses = sorted(r.status for r in rows)
    assert statuses == ["pending", "sent"]
    # The metric fires only AFTER a successful commit: exactly one increment, even
    # though both rows' Telnyx sends succeeded.
    assert _counter_value(SMS_MESSAGES_TOTAL, status="sent") == before + 1
    get_settings.cache_clear()


def test_flush_never_raises_on_unexpected_error(client, async_database_url, monkeypatch, request):
    # An error before the per-row loop (e.g. the pending-rows query) used to
    # propagate into FastAPI's BackgroundTasks runner, which swallows it silently —
    # rows stuck pending, nothing logged. flush_pending_sms must catch everything
    # itself and surface the failure via its own (PHI-free) log line.
    _patch_factory(request, monkeypatch, async_database_url)
    invoked = {"n": 0}

    async def _boom(db, call_id):
        invoked["n"] += 1
        raise RuntimeError("db exploded")

    monkeypatch.setattr(sms_outbox.sms_repo, "get_pending_for_call", _boom)

    from loguru import logger as loguru_logger

    records: list = []
    handler_id = loguru_logger.add(records.append, level="ERROR")
    try:
        asyncio.run(sms_outbox.flush_pending_sms(uuid.uuid4()))  # must not raise
    finally:
        loguru_logger.remove(handler_id)
    assert invoked["n"] == 1  # the failure actually came from the patched query
    crash = next(m for m in records if "SMS flush crashed" in m.record["message"])
    # PHI rule: only the exception TYPE is logged, never str(exc) (an asyncpg/httpx
    # message can embed SQL params or URLs tied to contact phone numbers).
    assert crash.record["extra"]["err"] == "RuntimeError"
    assert "db exploded" not in str(crash)
