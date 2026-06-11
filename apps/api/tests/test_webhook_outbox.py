"""webhook_outbox enqueue: same-transaction fan-out to enabled+subscribed endpoints.

Pins the transactional-outbox contract (spec §2.1): the enqueue joins the
caller's transaction (flush-only — visible to other connections only after the
caller's commit; rollback discards business change and event together), fans
out one delivery row per ENABLED endpoint SUBSCRIBED to the event, inserts
zero rows at zero cost when no endpoint qualifies (the ship-inert posture),
and `enqueue_ping` bypasses subscriptions entirely (the /test pipeline, §4/§10.8).
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.models import WebhookDelivery, WebhookEndpoint
from usan_api.repositories import webhook_outbox

PAYLOAD = {
    "event": "call.completed",
    "occurred_at": "2026-06-10T12:00:00+00:00",
    "data": {"call_id": "00000000-0000-0000-0000-000000000001", "status": "completed"},
}


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    # Wraps the test (before AND after): leftover webhook_endpoints rows would
    # change enqueue fan-out behavior in unrelated modules once the call
    # mutators are instrumented (Task C4).
    async def _wipe():
        async with session_factory() as db:
            await db.execute(text("TRUNCATE webhook_deliveries, webhook_endpoints CASCADE"))
            await db.commit()

    await _wipe()
    yield
    await _wipe()


def _endpoint(*, enabled: bool = True, events: list[str] | None = None) -> WebhookEndpoint:
    # Seeded via the model directly — the operator CRUD repo does not exist yet (Task D2).
    return WebhookEndpoint(
        url="https://hooks.example.com/sink",
        secret="a" * 64,
        enabled=enabled,
        events=events if events is not None else ["call.completed"],
    )


async def _delivery_count(db) -> int:
    result = await db.execute(select(func.count()).select_from(WebhookDelivery))
    return int(result.scalar_one())


async def test_enqueue_fans_out_to_enabled_subscribed_only(session_factory):
    async with session_factory() as db:
        subscribed = _endpoint(events=["call.completed"])
        other_event = _endpoint(events=["flag.created"])
        disabled = _endpoint(enabled=False, events=["call.completed"])
        db.add_all([subscribed, other_event, disabled])
        await db.flush()
        subscribed_id = subscribed.id

        count = await webhook_outbox.enqueue_event(db, event="call.completed", payload=PAYLOAD)
        await db.commit()

    assert count == 1
    async with session_factory() as db:
        rows = list((await db.execute(select(WebhookDelivery))).scalars().all())
        assert len(rows) == 1
        row = rows[0]
        assert row.endpoint_id == subscribed_id
        assert row.status == "pending"
        assert row.attempts == 0
        assert row.payload == PAYLOAD


async def test_enqueue_zero_endpoints_zero_rows(session_factory):
    async with session_factory() as db:
        count = await webhook_outbox.enqueue_event(db, event="call.completed", payload=PAYLOAD)
        await db.commit()

    assert count == 0
    async with session_factory() as db:
        assert await _delivery_count(db) == 0


async def test_enqueue_same_txn_visibility(session_factory, async_database_url):
    async with session_factory() as db:
        db.add(_endpoint(events=["call.completed"]))
        await db.commit()

    probe_engine = create_async_engine(async_database_url, poolclass=NullPool)
    probe_factory = async_sessionmaker(probe_engine, expire_on_commit=False)
    try:
        async with session_factory() as db:
            count = await webhook_outbox.enqueue_event(db, event="call.completed", payload=PAYLOAD)
            assert count == 1
            # Flushed but UNCOMMITTED: a second engine's session must see nothing —
            # the outbox row joins the caller's transaction (spec §2.1).
            async with probe_factory() as probe:
                assert await _delivery_count(probe) == 0
            await db.commit()

        async with probe_factory() as probe:
            assert await _delivery_count(probe) == 1
    finally:
        await probe_engine.dispose()


async def test_enqueue_rollback_leaves_nothing(session_factory):
    async with session_factory() as db:
        db.add(_endpoint(events=["call.completed"]))
        await db.commit()

    async with session_factory() as db:
        count = await webhook_outbox.enqueue_event(db, event="call.completed", payload=PAYLOAD)
        assert count == 1
        await db.rollback()

    # Crash-between ⇒ NEITHER business change nor event (spec §10.7).
    async with session_factory() as db:
        assert await _delivery_count(db) == 0


async def test_enqueue_ping_ignores_subscriptions(session_factory):
    async with session_factory() as db:
        endpoint = _endpoint(events=["flag.created"])  # NOT subscribed to ping (nobody can be)
        db.add(endpoint)
        await db.flush()
        endpoint_id = endpoint.id

        row = await webhook_outbox.enqueue_ping(db, endpoint_id=endpoint_id)
        await db.commit()

    assert isinstance(row.id, uuid.UUID)
    assert row.endpoint_id == endpoint_id
    assert row.event == "ping"
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.payload["event"] == "ping"
    assert row.payload["data"] == {"endpoint_id": str(endpoint_id)}
    # Immediately due: the /test pipeline expects next_attempt_at = now() (spec §4).
    assert row.next_attempt_at <= datetime.now(UTC)

    async with session_factory() as db:
        assert await _delivery_count(db) == 1
