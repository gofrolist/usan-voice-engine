"""T005 + T089 (US-foundational): the notification outbox poller.

Flushes `call_id IS NULL` pending notification rows via Telnyx, idempotently
(status-guarded), with a per-row commit so a mid-batch failure cannot re-send the
whole batch. It must NOT touch in-call SMS rows (call_id set) — those are flushed
by sms_outbox. T089 pins the SC-004 latency budget: the poll interval is the
worst-case dispatch latency and is capped at the 5-minute budget.
"""

import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import counter_value as _counter_value
from usan_api import notification_outbox, notifications, telnyx_messaging
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import SmsMessage
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import sms_messages as sms_repo

_FAMILY_NUMBER = "+15557654321"


def _engine(url: str):
    return create_async_engine(url, poolclass=NullPool)


def _patch_factory(request: pytest.FixtureRequest, monkeypatch, url: str) -> None:
    # flush opens its own session via the module-global cached engine; point it at a
    # NullPool factory so each asyncio.run() loop gets a fresh connection (mirrors
    # test_sms_outbox._patch_factory).
    engine = _engine(url)
    request.addfinalizer(lambda: asyncio.run(engine.dispose()))
    monkeypatch.setattr(
        notification_outbox,
        "get_session_factory",
        lambda: async_sessionmaker(engine, expire_on_commit=False),
    )


def _enable_messaging(monkeypatch) -> None:
    monkeypatch.setenv("TELNYX_MESSAGING_ENABLED", "true")
    monkeypatch.setenv("TELNYX_MESSAGING_API_KEY", "KEY")
    monkeypatch.setenv("TELNYX_MESSAGING_PROFILE_ID", "mp1")
    monkeypatch.setenv("TELNYX_FROM_NUMBER", "+15551230000")
    from usan_api.settings import get_settings

    get_settings.cache_clear()


async def _seed_notification(url: str, *, dedupe_key: str | None = None) -> uuid.UUID:
    engine = _engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            contact = await contacts_repo.create_contact(
                db, name="Ada", phone_e164=f"+1555{str(uuid.uuid4().int)[:7]}", timezone="UTC"
            )
            row = await notifications.enqueue_family_alert(
                db,
                contact_id=contact.id,
                to_number=_FAMILY_NUMBER,
                reason="crisis",
                dedupe_key=dedupe_key or f"crisis:{uuid.uuid4()}",
            )
            await db.commit()
            return row.id
    finally:
        await engine.dispose()


async def _seed_in_call_sms(url: str) -> uuid.UUID:
    """A normal in-call sms row (call_id set). The notification outbox must ignore it."""
    engine = _engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            contact = await contacts_repo.create_contact(
                db, name="Bo", phone_e164=f"+1555{str(uuid.uuid4().int)[:7]}", timezone="UTC"
            )
            call = await calls_repo.create_call(
                db,
                contact_id=contact.id,
                direction=CallDirection.OUTBOUND,
                status=CallStatus.IN_PROGRESS,
            )
            row = await sms_repo.create_sms_message(
                db,
                call_id=call.id,
                contact_id=contact.id,
                to_number=contact.phone_e164,
                template_key="greet",
                body="hi",
            )
            await db.commit()
            return row.id
    finally:
        await engine.dispose()


async def _row(url: str, sms_id: uuid.UUID) -> SmsMessage | None:
    engine = _engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            return await db.get(SmsMessage, sms_id)
    finally:
        await engine.dispose()


def test_outbox_flushes_pending_notification(client, async_database_url, monkeypatch, request):
    _enable_messaging(monkeypatch)

    async def _fake_send(settings, *, to_number, body):
        return "msg-fam-1"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _fake_send)
    _patch_factory(request, monkeypatch, async_database_url)

    from usan_api.observability.custom_metrics import SMS_MESSAGES_TOTAL

    before = _counter_value(SMS_MESSAGES_TOTAL, status="sent")
    sms_id = asyncio.run(_seed_notification(async_database_url))
    asyncio.run(notification_outbox.flush_pending_notifications())
    row = asyncio.run(_row(async_database_url, sms_id))
    assert row is not None
    assert row.status == "sent"
    assert row.telnyx_message_id == "msg-fam-1"
    assert _counter_value(SMS_MESSAGES_TOTAL, status="sent") == before + 1
    from usan_api.settings import get_settings

    get_settings.cache_clear()


def test_outbox_ignores_in_call_sms_rows(client, async_database_url, monkeypatch, request):
    # A notification (call_id NULL) and an in-call sms (call_id set) both pending. The
    # notification outbox must claim ONLY the notification; the in-call row stays pending.
    _enable_messaging(monkeypatch)

    async def _fake_send(settings, *, to_number, body):
        return f"msg-{uuid.uuid4().hex[:6]}"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _fake_send)
    _patch_factory(request, monkeypatch, async_database_url)

    notif_id = asyncio.run(_seed_notification(async_database_url))
    in_call_id = asyncio.run(_seed_in_call_sms(async_database_url))
    asyncio.run(notification_outbox.flush_pending_notifications())
    notif = asyncio.run(_row(async_database_url, notif_id))
    in_call = asyncio.run(_row(async_database_url, in_call_id))
    assert notif.status == "sent"
    assert in_call.status == "pending"  # untouched by the notification outbox
    from usan_api.settings import get_settings

    get_settings.cache_clear()


def test_outbox_idempotent_reflush(client, async_database_url, monkeypatch, request):
    _enable_messaging(monkeypatch)

    async def _fake_send(settings, *, to_number, body):
        return "msg-once"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _fake_send)
    _patch_factory(request, monkeypatch, async_database_url)

    from usan_api.observability.custom_metrics import SMS_MESSAGES_TOTAL

    before = _counter_value(SMS_MESSAGES_TOTAL, status="sent")
    sms_id = asyncio.run(_seed_notification(async_database_url))
    asyncio.run(notification_outbox.flush_pending_notifications())
    asyncio.run(notification_outbox.flush_pending_notifications())  # re-flush: no-op
    row = asyncio.run(_row(async_database_url, sms_id))
    assert row.status == "sent"
    assert _counter_value(SMS_MESSAGES_TOTAL, status="sent") == before + 1  # not double-counted
    from usan_api.settings import get_settings

    get_settings.cache_clear()


def test_outbox_leaves_pending_when_messaging_disabled(
    client, async_database_url, monkeypatch, request
):
    # A crisis/missed-call alert must NOT be burned when messaging is misconfigured-off:
    # it stays pending so the backlog flushes once messaging is enabled.
    from usan_api.settings import get_settings

    get_settings.cache_clear()  # messaging disabled by default (client env)
    _patch_factory(request, monkeypatch, async_database_url)
    sms_id = asyncio.run(_seed_notification(async_database_url))
    asyncio.run(notification_outbox.flush_pending_notifications())
    row = asyncio.run(_row(async_database_url, sms_id))
    assert row.status == "pending"
    get_settings.cache_clear()


def _patch_factory_failing_commit(
    request: pytest.FixtureRequest, monkeypatch, url: str, *, fail_on_call: int
) -> None:
    engine = _engine(url)
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

    monkeypatch.setattr(notification_outbox, "get_session_factory", lambda: factory)


def test_outbox_per_row_commit_survives_partial_failure(
    client, async_database_url, monkeypatch, request
):
    # Two pending notifications; commit after the SECOND fails. Per-row commits mean the
    # first's `sent` status survives (the duplicate window is at most the one failed row).
    _enable_messaging(monkeypatch)

    async def _fake_send(settings, *, to_number, body):
        return f"msg-{uuid.uuid4().hex[:6]}"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _fake_send)
    _patch_factory_failing_commit(request, monkeypatch, async_database_url, fail_on_call=2)

    id1 = asyncio.run(_seed_notification(async_database_url))
    id2 = asyncio.run(_seed_notification(async_database_url))
    asyncio.run(notification_outbox.flush_pending_notifications())  # must not raise
    r1 = asyncio.run(_row(async_database_url, id1))
    r2 = asyncio.run(_row(async_database_url, id2))
    statuses = sorted([r1.status, r2.status])
    assert statuses == ["pending", "sent"]
    from usan_api.settings import get_settings

    get_settings.cache_clear()


def test_family_alert_dispatch_latency_within_sc004_budget(
    client, async_database_url, monkeypatch, request
):
    # T089 / SC-004: a family alert must be delivered within 5 minutes. The poll interval
    # is the worst-case dispatch latency, so the configured interval must be <= 300s, and
    # a single flush must dispatch a pending alert (so it lands within one interval).
    from usan_api.settings import get_settings

    _enable_messaging(monkeypatch)
    settings = get_settings()
    assert settings.notification_outbox_poll_interval_s <= 300

    async def _fake_send(settings, *, to_number, body):
        return "msg-latency"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _fake_send)
    _patch_factory(request, monkeypatch, async_database_url)
    sms_id = asyncio.run(_seed_notification(async_database_url))
    asyncio.run(notification_outbox.flush_pending_notifications())  # one poll cycle
    row = asyncio.run(_row(async_database_url, sms_id))
    assert row.status == "sent"  # dispatched within a single interval (<= 5 min)
    get_settings.cache_clear()


# --- review H1 (TCPA opt-out at send) + H2 (FOR UPDATE SKIP LOCKED claim) -------------------


async def _seed_notification_to(url: str, *, to_number: str, kind: str, on_dnc: bool) -> uuid.UUID:
    engine = _engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            contact = await contacts_repo.create_contact(
                db, name="Ada", phone_e164=f"+1555{str(uuid.uuid4().int)[:7]}", timezone="UTC"
            )
            if on_dnc:
                await dnc_repo.add_entry(db, to_number, "test opt-out")
            row = await sms_repo.create_notification(
                db,
                contact_id=contact.id,
                to_number=to_number,
                kind=kind,
                body="USAN update.",
                dedupe_key=f"{kind}:{uuid.uuid4()}",
            )
            await db.commit()
            assert row is not None
            return row.id
    finally:
        await engine.dispose()


def test_outbox_suppresses_sms_to_opted_out_number(
    client, async_database_url, monkeypatch, request
):
    # H1 / TCPA: a number that texted STOP (on the DNC list) must NOT receive family
    # alerts/reports — the outbox marks the row 'suppressed' and never transmits.
    _enable_messaging(monkeypatch)
    sent: list[str] = []

    async def _fake_send(settings, *, to_number, body):
        sent.append(to_number)
        return "msg-should-not-happen"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _fake_send)
    _patch_factory(request, monkeypatch, async_database_url)

    blocked = f"+1555{str(uuid.uuid4().int)[:7]}"
    sms_id = asyncio.run(
        _seed_notification_to(
            async_database_url, to_number=blocked, kind="family_alert", on_dnc=True
        )
    )
    asyncio.run(notification_outbox.flush_pending_notifications())
    row = asyncio.run(_row(async_database_url, sms_id))
    assert row.status == "suppressed"
    assert blocked not in sent  # never transmitted to the opted-out number
    from usan_api.settings import get_settings

    get_settings.cache_clear()


def test_outbox_sends_opt_out_ack_even_to_blocked_number(
    client, async_database_url, monkeypatch, request
):
    # H1 exemption: the one-time opt-out acknowledgement is TCPA-permitted and goes TO the
    # number that just opted out (now on the DNC list) — it must still send.
    _enable_messaging(monkeypatch)
    sent: list[str] = []

    async def _fake_send(settings, *, to_number, body):
        sent.append(to_number)
        return "msg-ack"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _fake_send)
    _patch_factory(request, monkeypatch, async_database_url)

    blocked = f"+1555{str(uuid.uuid4().int)[:7]}"
    sms_id = asyncio.run(
        _seed_notification_to(
            async_database_url, to_number=blocked, kind="opt_out_ack", on_dnc=True
        )
    )
    asyncio.run(notification_outbox.flush_pending_notifications())
    row = asyncio.run(_row(async_database_url, sms_id))
    assert row.status == "sent"
    assert blocked in sent  # the confirmation is delivered despite the DNC entry
    from usan_api.settings import get_settings

    get_settings.cache_clear()


async def test_claim_pending_notification_skips_locked_row(async_database_url):
    # H2: a pending row locked by one transaction (FOR UPDATE SKIP LOCKED) is SKIPPED — not
    # blocked on — by a concurrent claim, so two replicas never claim (and send) the same row.
    engine = _engine(async_database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            contact = await contacts_repo.create_contact(
                db, name="Lock", phone_e164=f"+1555{str(uuid.uuid4().int)[:7]}", timezone="UTC"
            )
            await sms_repo.create_notification(
                db,
                contact_id=contact.id,
                to_number=f"+1555{str(uuid.uuid4().int)[:7]}",
                kind="family_alert",
                body="x",
                dedupe_key=f"lock:{uuid.uuid4()}",
            )
            await db.commit()

        async with factory() as a, factory() as b:
            claimed_a = await sms_repo.claim_pending_notification(a)  # locks a pending row
            assert claimed_a is not None
            claimed_b = await sms_repo.claim_pending_notification(b)  # must SKIP the locked row
            # b never gets the row a holds (it may get an unrelated pending row, or None).
            assert claimed_b is None or claimed_b.id != claimed_a.id
            await a.rollback()
    finally:
        await engine.dispose()
