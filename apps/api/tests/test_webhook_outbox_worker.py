"""Outbox worker half: claim lease, guarded outcomes, housekeeping (Task E1).

Pins the spec §5.2 attempt-bump claim (a crash-safe lease — claiming
pre-schedules the next rung, so a worker crash needs no reclaim sweeper), the
§5.3 guarded outcome writes, and the §5.4 housekeeping sweeps. Time travel is
done by binding ``now`` into the repo functions (the ``:now`` parametrization
exists precisely for these tests) plus raw UPDATEs of row timestamps.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.models import WebhookDelivery, WebhookEndpoint
from usan_api.repositories import webhook_outbox

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)

PAYLOAD: dict[str, Any] = {"event": "ping", "occurred_at": "2026-06-10T12:00:00+00:00", "data": {}}


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    # Wraps the test (before AND after): leftover webhook_endpoints rows change
    # enqueue fan-out behavior in unrelated modules (the C4-instrumented mutators).
    async def _wipe():
        async with session_factory() as db:
            await db.execute(text("TRUNCATE webhook_deliveries, webhook_endpoints CASCADE"))
            await db.commit()

    await _wipe()
    yield
    await _wipe()


@pytest.fixture
async def factories(async_database_url):
    """Two engines => two distinct connections (genuine SKIP LOCKED contention)."""
    engine_a = create_async_engine(async_database_url, poolclass=NullPool)
    engine_b = create_async_engine(async_database_url, poolclass=NullPool)
    yield (
        async_sessionmaker(engine_a, expire_on_commit=False),
        async_sessionmaker(engine_b, expire_on_commit=False),
    )
    await engine_a.dispose()
    await engine_b.dispose()


def _endpoint_model(*, enabled: bool = True) -> WebhookEndpoint:
    return WebhookEndpoint(
        url="https://hooks.example.com/sink",
        secret="a" * 64,
        enabled=enabled,
        events=["call.completed"],
    )


def _delivery_model(
    endpoint_id: uuid.UUID,
    *,
    status: str = "pending",
    attempts: int = 0,
    response_code: int | None = None,
    last_error: str | None = None,
) -> WebhookDelivery:
    return WebhookDelivery(
        endpoint_id=endpoint_id,
        event="ping",
        payload=PAYLOAD,
        status=status,
        attempts=attempts,
        response_code=response_code,
        last_error=last_error,
    )


async def _seed(
    factory, *, enabled: bool = True, rows: list[WebhookDelivery] | None = None
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    async with factory() as db:
        endpoint = _endpoint_model(enabled=enabled)
        db.add(endpoint)
        await db.flush()
        deliveries = rows if rows is not None else [_delivery_model(endpoint.id)]
        for row in deliveries:
            row.endpoint_id = endpoint.id
            db.add(row)
        await db.commit()
        return endpoint.id, [row.id for row in deliveries]


async def _set_row(factory, delivery_id: uuid.UUID, **cols: object) -> None:
    """Raw time travel. Explicit ``updated_at`` (when given) wins over onupdate."""
    async with factory() as db:
        await db.execute(
            update(WebhookDelivery).where(WebhookDelivery.id == delivery_id).values(**cols)
        )
        await db.commit()


async def _get_row(factory, delivery_id: uuid.UUID) -> WebhookDelivery:
    async with factory() as db:
        row = await db.get(WebhookDelivery, delivery_id)
        assert row is not None
        return row


async def _claim(factory, *, now: datetime, limit: int = 20) -> list:
    async with factory() as db:
        claimed = await webhook_outbox.claim_due(db, now=now, limit=limit)
        await db.commit()
    return claimed


def _close_to(actual: datetime, expected: datetime) -> bool:
    return abs(actual - expected) < timedelta(seconds=1)


async def test_claim_bumps_attempts_and_ladder(session_factory):
    endpoint_id, (d_id,) = await _seed(session_factory)
    await _set_row(session_factory, d_id, next_attempt_at=NOW - timedelta(seconds=1))

    # Rung 1: claim bumps 0 -> 1 and pre-schedules +1m (the lease).
    claimed = await _claim(session_factory, now=NOW)
    assert [(c.id, c.endpoint_id, c.event, c.attempts) for c in claimed] == [
        (d_id, endpoint_id, "ping", 1)
    ]
    assert claimed[0].payload == PAYLOAD
    row = await _get_row(session_factory, d_id)
    assert row.status == "pending"
    assert row.attempts == 1
    assert _close_to(row.next_attempt_at, NOW + timedelta(minutes=1))

    # Rung 2: +5m.
    t2 = NOW + timedelta(minutes=2)
    claimed = await _claim(session_factory, now=t2)
    assert [c.attempts for c in claimed] == [2]
    row = await _get_row(session_factory, d_id)
    assert _close_to(row.next_attempt_at, t2 + timedelta(minutes=5))

    # Rung 3: +30m.
    t3 = t2 + timedelta(minutes=6)
    claimed = await _claim(session_factory, now=t3)
    assert [c.attempts for c in claimed] == [3]
    row = await _get_row(session_factory, d_id)
    assert _close_to(row.next_attempt_at, t3 + timedelta(minutes=30))

    # Rung 4 (last): +30m again.
    t4 = t3 + timedelta(minutes=31)
    claimed = await _claim(session_factory, now=t4)
    assert [c.attempts for c in claimed] == [4]
    row = await _get_row(session_factory, d_id)
    assert _close_to(row.next_attempt_at, t4 + timedelta(minutes=30))

    # attempts=4 is never claimed again (the `attempts < 4` predicate).
    t5 = t4 + timedelta(hours=2)
    assert await _claim(session_factory, now=t5) == []
    row = await _get_row(session_factory, d_id)
    assert row.status == "pending"
    assert row.attempts == 4


async def test_claim_skips_disabled_endpoints(session_factory):
    endpoint_id, (d_id,) = await _seed(
        session_factory,
        enabled=False,
        rows=[_delivery_model(uuid.uuid4(), attempts=2)],
    )
    await _set_row(session_factory, d_id, next_attempt_at=NOW - timedelta(minutes=1))

    # Disabled endpoint: the row is due but never claimed (the JOIN ... AND e.enabled).
    assert await _claim(session_factory, now=NOW) == []

    async with session_factory() as db:
        await db.execute(
            update(WebhookEndpoint).where(WebhookEndpoint.id == endpoint_id).values(enabled=True)
        )
        await db.commit()

    # Re-enable: claimed with its attempt count intact (2 -> 3).
    claimed = await _claim(session_factory, now=NOW)
    assert [(c.id, c.attempts) for c in claimed] == [(d_id, 3)]


async def test_claim_orders_oldest_due_first_and_limit(session_factory):
    _, ids = await _seed(session_factory, rows=[_delivery_model(uuid.uuid4()) for _ in range(3)])
    # Deliberately decoupled from insert order: ids[1] is the oldest due.
    await _set_row(session_factory, ids[0], next_attempt_at=NOW - timedelta(minutes=2))
    await _set_row(session_factory, ids[1], next_attempt_at=NOW - timedelta(minutes=3))
    await _set_row(session_factory, ids[2], next_attempt_at=NOW - timedelta(minutes=1))

    claimed = await _claim(session_factory, now=NOW, limit=2)
    assert [c.id for c in claimed] == [ids[1], ids[0]]

    claimed = await _claim(session_factory, now=NOW)
    assert [c.id for c in claimed] == [ids[2]]


async def test_claim_skip_locked_disjoint(factories):
    factory_a, factory_b = factories
    _, ids = await _seed(factory_a, rows=[_delivery_model(uuid.uuid4()) for _ in range(2)])
    await _set_row(factory_a, ids[0], next_attempt_at=NOW - timedelta(minutes=2))
    await _set_row(factory_a, ids[1], next_attempt_at=NOW - timedelta(minutes=1))

    async with factory_a() as db_a:
        claimed_a = await webhook_outbox.claim_due(db_a, now=NOW, limit=1)
        assert [c.id for c in claimed_a] == [ids[0]]  # A holds the row lock (txn open)
        async with factory_b() as db_b:
            # B skips A's locked row instead of blocking (FOR UPDATE OF d SKIP LOCKED).
            claimed_b = await webhook_outbox.claim_due(db_b, now=NOW, limit=20)
            assert [c.id for c in claimed_b] == [ids[1]]
            await db_b.commit()
        await db_a.rollback()

    # A rolled back: its row is untouched and claimable again.
    row = await _get_row(factory_a, ids[0])
    assert row.attempts == 0
    claimed = await _claim(factory_a, now=NOW)
    assert [(c.id, c.attempts) for c in claimed] == [(ids[0], 1)]


async def test_crash_after_claim_reoffers_at_next_rung(session_factory):
    _, (d_id,) = await _seed(session_factory)
    await _set_row(session_factory, d_id, next_attempt_at=NOW - timedelta(seconds=1))

    # Claim, commit, then "crash": no outcome is ever written.
    claimed = await _claim(session_factory, now=NOW)
    assert [c.attempts for c in claimed] == [1]

    # The row stays pending — but the bumped lease is not yet due.
    row = await _get_row(session_factory, d_id)
    assert row.status == "pending"
    assert await _claim(session_factory, now=NOW + timedelta(seconds=30)) == []

    # Once now passes the pre-scheduled rung it re-offers automatically
    # (crash-safe lease; no reclaim sweeper needed, §5.2/§5.4).
    claimed = await _claim(session_factory, now=NOW + timedelta(minutes=2))
    assert [(c.id, c.attempts) for c in claimed] == [(d_id, 2)]


async def test_mark_delivered_guarded_idempotent(session_factory):
    _, (d_id,) = await _seed(session_factory)

    async with session_factory() as db:
        assert await webhook_outbox.mark_delivered(db, d_id, response_code=200) is True
        await db.commit()

    row = await _get_row(session_factory, d_id)
    assert row.status == "delivered"
    assert row.delivered_at is not None
    assert row.response_code == 200
    first_delivered_at = row.delivered_at

    # Second call: guarded on status='pending' -> False, row untouched.
    async with session_factory() as db:
        assert await webhook_outbox.mark_delivered(db, d_id, response_code=204) is False
        await db.commit()

    row = await _get_row(session_factory, d_id)
    assert row.status == "delivered"
    assert row.response_code == 200
    assert row.delivered_at == first_delivered_at


async def test_mark_attempt_failed_guarded_and_terminal(session_factory):
    _, ids = await _seed(session_factory, rows=[_delivery_model(uuid.uuid4()) for _ in range(2)])
    retry_id, delivered_id = ids

    # Non-terminal failure: outcome recorded, row stays pending (lease re-offers it).
    async with session_factory() as db:
        assert (
            await webhook_outbox.mark_attempt_failed(
                db, retry_id, response_code=500, last_error="HTTPStatusError", terminal=False
            )
            is True
        )
        await db.commit()
    row = await _get_row(session_factory, retry_id)
    assert row.status == "pending"
    assert row.response_code == 500
    assert row.last_error == "HTTPStatusError"

    # Terminal failure: status flips to failed.
    async with session_factory() as db:
        assert (
            await webhook_outbox.mark_attempt_failed(
                db, retry_id, response_code=None, last_error="ConnectTimeout", terminal=True
            )
            is True
        )
        await db.commit()
    row = await _get_row(session_factory, retry_id)
    assert row.status == "failed"
    assert row.response_code is None
    assert row.last_error == "ConnectTimeout"

    # Guarded exactly like mark_delivered (review L3): a delivered row is never failed.
    async with session_factory() as db:
        assert await webhook_outbox.mark_delivered(db, delivered_id, response_code=200) is True
        assert (
            await webhook_outbox.mark_attempt_failed(
                db, delivered_id, response_code=500, last_error="HTTPStatusError", terminal=True
            )
            is False
        )
        await db.commit()
    row = await _get_row(session_factory, delivered_id)
    assert row.status == "delivered"
    assert row.last_error is None


async def test_sweep_crash_residue_coalesces_last_error(session_factory):
    _, ids = await _seed(
        session_factory,
        rows=[
            _delivery_model(uuid.uuid4(), attempts=4, last_error="ConnectTimeout"),
            _delivery_model(uuid.uuid4(), attempts=4),
            _delivery_model(uuid.uuid4(), attempts=4),
            _delivery_model(uuid.uuid4(), attempts=3),
        ],
    )
    typed_id, residual_id, fresh_id, claimable_id = ids
    stale = NOW - timedelta(minutes=11)
    await _set_row(session_factory, typed_id, updated_at=stale)
    await _set_row(session_factory, residual_id, updated_at=stale)
    await _set_row(session_factory, fresh_id, updated_at=NOW - timedelta(minutes=5))
    await _set_row(session_factory, claimable_id, updated_at=stale)

    async with session_factory() as db:
        assert await webhook_outbox.sweep_crash_residue(db, now=NOW) == 2
        await db.commit()

    # A genuine last error type is never overwritten (COALESCE, review L4d) ...
    row = await _get_row(session_factory, typed_id)
    assert row.status == "failed"
    assert row.last_error == "ConnectTimeout"
    # ... while NULL becomes the sentinel.
    row = await _get_row(session_factory, residual_id)
    assert row.status == "failed"
    assert row.last_error == "crash_residual"
    # 10-minute grace (a live worker may still be mid-POST) and attempts<4
    # (still claimable -> not residue) are both respected.
    assert (await _get_row(session_factory, fresh_id)).status == "pending"
    assert (await _get_row(session_factory, claimable_id)).status == "pending"


async def test_expire_stale_pending_after_7_days(session_factory):
    _, ids = await _seed(session_factory, rows=[_delivery_model(uuid.uuid4()) for _ in range(2)])
    stale_id, recent_id = ids
    await _set_row(session_factory, stale_id, created_at=NOW - timedelta(days=8))
    await _set_row(session_factory, recent_id, created_at=NOW - timedelta(days=6))

    async with session_factory() as db:
        assert await webhook_outbox.expire_stale_pending(db, now=NOW) == 1
        await db.commit()

    row = await _get_row(session_factory, stale_id)
    assert row.status == "failed"
    assert row.last_error == "expired"
    assert (await _get_row(session_factory, recent_id)).status == "pending"


async def test_prune_old_30_days(session_factory):
    _, ids = await _seed(
        session_factory,
        rows=[
            _delivery_model(uuid.uuid4(), status="delivered", attempts=1, response_code=200),
            _delivery_model(uuid.uuid4(), status="failed", attempts=4, last_error="expired"),
            _delivery_model(uuid.uuid4(), status="delivered", attempts=1, response_code=200),
            _delivery_model(uuid.uuid4()),
        ],
    )
    old_delivered, old_failed, recent_delivered, old_pending = ids
    await _set_row(session_factory, old_delivered, created_at=NOW - timedelta(days=31))
    await _set_row(session_factory, old_failed, created_at=NOW - timedelta(days=31))
    await _set_row(session_factory, recent_delivered, created_at=NOW - timedelta(days=29))
    await _set_row(session_factory, old_pending, created_at=NOW - timedelta(days=31))

    async with session_factory() as db:
        assert await webhook_outbox.count_pending(db) == 1
        assert await webhook_outbox.prune_old(db, now=NOW) == 2
        await db.commit()

    async with session_factory() as db:
        assert await db.get(WebhookDelivery, old_delivered) is None
        assert await db.get(WebhookDelivery, old_failed) is None
        assert await db.get(WebhookDelivery, recent_delivered) is not None
        # Old PENDING rows are expire_stale_pending's job, never prune's.
        assert await db.get(WebhookDelivery, old_pending) is not None
        # The backlog count the poller reports every cycle (§9) is unchanged.
        assert await webhook_outbox.count_pending(db) == 1
