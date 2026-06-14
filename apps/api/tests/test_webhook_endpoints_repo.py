"""webhook_endpoints repo + outbox redeliver/pending reads (Task D3).

Pins the operator CRUD surface (spec §4), the atomic-SQL circuit-breaker
mutators (spec §5.5 — an ORM read-modify-write would lose updates against a
concurrent PATCH ``enabled=true`` reset and could double-fire the trip WARN),
and the guarded redeliver reset (spec §4 — the status predicate is
load-bearing; a Python status check would race the poller's in-flight claim).

The breaker exactly-once claims are about RACES, not sequences (spec §10.11),
so the trip/re-enable/concurrent-trip tests use the genuine two-engine
open-transaction interleaving from ``test_calls_guarded_transitions.py``:
session A mutates (flushed, transaction held open), session B blocks on the
row lock, A commits, B proceeds.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.models import WebhookDelivery, WebhookEndpoint
from usan_api.repositories import webhook_endpoints as endpoints_repo
from usan_api.repositories import webhook_outbox

Mutator = Callable[[AsyncSession], Awaitable[object]]

PAYLOAD: dict[str, Any] = {"event": "ping", "occurred_at": "2026-06-10T12:00:00+00:00", "data": {}}


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
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
    """Two engines => two distinct connections (genuine row-lock contention)."""
    engine_a = create_async_engine(async_database_url, poolclass=NullPool)
    engine_b = create_async_engine(async_database_url, poolclass=NullPool)
    yield (
        async_sessionmaker(engine_a, expire_on_commit=False),
        async_sessionmaker(engine_b, expire_on_commit=False),
    )
    await engine_a.dispose()
    await engine_b.dispose()


def _endpoint_model(
    *,
    enabled: bool = True,
    consecutive_failures: int = 0,
    disabled_reason: str | None = None,
) -> WebhookEndpoint:
    return WebhookEndpoint(
        url="https://hooks.example.com/sink",
        secret="a" * 64,
        enabled=enabled,
        events=["call.completed"],
        consecutive_failures=consecutive_failures,
        disabled_reason=disabled_reason,
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


async def _seed_endpoint(factory, endpoint: WebhookEndpoint) -> uuid.UUID:
    async with factory() as db:
        db.add(endpoint)
        await db.commit()
        return endpoint.id


async def _get_endpoint(factory, endpoint_id: uuid.UUID) -> WebhookEndpoint:
    async with factory() as db:
        endpoint = await endpoints_repo.get_endpoint(db, endpoint_id)
        assert endpoint is not None
        return endpoint


async def _race(factory_a, factory_b, mutator_a: Mutator, mutator_b: Mutator):
    """The §10.11 interleaving: A mutates (flushed, transaction held open), B
    starts on the second connection and must block on the row lock,
    A commits, then B completes.
    """

    async def _run_b() -> object:
        async with factory_b() as db:
            result = await mutator_b(db)
            await db.commit()
            return result

    async with factory_a() as db_a:
        result_a = await mutator_a(db_a)
        task = asyncio.create_task(_run_b())
        await asyncio.sleep(0.2)  # let B reach (and block on) the locked row
        await db_a.commit()
    result_b = await task
    return result_a, result_b


async def test_create_get_list_delete_and_count(session_factory):
    async with session_factory() as db:
        assert await endpoints_repo.count_endpoints(db) == 0
        created = await endpoints_repo.create_endpoint(
            db,
            url="https://hooks.example.com/sink",
            description="ops sink",
            events=["call.completed", "call.started"],
            secret="b" * 64,
        )
        await db.commit()
        endpoint_id = created.id

    assert created.enabled is True  # server defaults materialized by refresh
    assert created.consecutive_failures == 0
    assert created.disabled_reason is None

    async with session_factory() as db:
        got = await endpoints_repo.get_endpoint(db, endpoint_id)
        assert got is not None
        assert got.url == "https://hooks.example.com/sink"
        assert got.events == ["call.completed", "call.started"]
        assert [e.id for e in await endpoints_repo.list_endpoints(db)] == [endpoint_id]
        assert await endpoints_repo.count_endpoints(db) == 1
        assert await endpoints_repo.get_endpoint(db, uuid.uuid4()) is None
        db.add(_delivery_model(endpoint_id))  # pending backlog must cascade on delete
        await db.commit()

    async with session_factory() as db:
        endpoint = await endpoints_repo.get_endpoint(db, endpoint_id)
        assert endpoint is not None
        await endpoints_repo.delete_endpoint(db, endpoint)
        await db.commit()

    async with session_factory() as db:
        assert await endpoints_repo.get_endpoint(db, endpoint_id) is None
        assert await endpoints_repo.count_endpoints(db) == 0
        # Behavioral half of the §10.2 cascade contract (metadata half in A2).
        deliveries = await db.execute(select(func.count()).select_from(WebhookDelivery))
        assert deliveries.scalar_one() == 0


async def test_pending_counts_per_endpoint(session_factory):
    async with session_factory() as db:
        e1 = _endpoint_model()
        e2 = _endpoint_model()
        db.add_all([e1, e2])
        await db.flush()
        e1_id, e2_id = e1.id, e2.id
        for _ in range(3):
            db.add(_delivery_model(e1_id))
        # Non-pending rows must not count toward backlog.
        db.add(_delivery_model(e1_id, status="delivered", attempts=1, response_code=200))
        await db.commit()

    async with session_factory() as db:
        assert await webhook_outbox.pending_counts(db) == {e1_id: 3}  # absent = 0
        assert await webhook_outbox.count_pending_for_endpoint(db, e1_id) == 3
        assert await webhook_outbox.count_pending_for_endpoint(db, e2_id) == 0


async def test_increment_failures_is_atomic_sql(factories):
    # increment racing a concurrent PATCH-reset must not lose the reset (§5.5).
    factory_a, factory_b = factories
    endpoint_id = await _seed_endpoint(factory_a, _endpoint_model())

    result_a, _ = await _race(
        factory_a,
        factory_b,
        lambda db: endpoints_repo.increment_failures(db, endpoint_id),
        lambda db: endpoints_repo.reset_failures(db, endpoint_id),
    )

    assert result_a == 1
    endpoint = await _get_endpoint(factory_a, endpoint_id)
    assert endpoint.consecutive_failures == 0  # the blocked reset wins as last writer

    async with factory_a() as db:
        # 1, not 2: the increment is SQL-side, never a stale read-modify-write.
        assert await endpoints_repo.increment_failures(db, endpoint_id) == 1
        await db.commit()


async def test_increment_skips_disabled_endpoint(session_factory):
    endpoint_id = await _seed_endpoint(session_factory, _endpoint_model(enabled=False))

    async with session_factory() as db:
        assert await endpoints_repo.increment_failures(db, endpoint_id) is None
        await db.commit()

    endpoint = await _get_endpoint(session_factory, endpoint_id)
    assert endpoint.consecutive_failures == 0


async def test_trip_breaker_one_shot(session_factory):
    endpoint_id = await _seed_endpoint(session_factory, _endpoint_model())

    async with session_factory() as db:
        assert await endpoints_repo.trip_breaker(db, endpoint_id) is True
        await db.commit()

    endpoint = await _get_endpoint(session_factory, endpoint_id)
    assert endpoint.enabled is False
    assert endpoint.disabled_reason == "circuit_breaker"

    async with session_factory() as db:
        # Guarded UPDATE: the second trip claims nothing (exactly-once WARN/metric).
        assert await endpoints_repo.trip_breaker(db, endpoint_id) is False
        await db.commit()


async def test_trip_vs_reenable_race(factories):
    # Concurrent, not sequential — the exactly-once claim is about races (§10.11).
    factory_a, factory_b = factories
    endpoint_id = await _seed_endpoint(factory_a, _endpoint_model(consecutive_failures=10))

    async def _reenable(db: AsyncSession) -> None:
        endpoint = await endpoints_repo.get_endpoint(db, endpoint_id)
        assert endpoint is not None
        await endpoints_repo.reenable(db, endpoint)

    result_a, _ = await _race(
        factory_a,
        factory_b,
        lambda db: endpoints_repo.trip_breaker(db, endpoint_id),
        _reenable,
    )

    assert result_a is True
    endpoint = await _get_endpoint(factory_a, endpoint_id)
    assert endpoint.enabled is True  # operator re-arm wins as last writer
    assert endpoint.consecutive_failures == 0
    assert endpoint.disabled_reason is None

    async with factory_a() as db:
        # The one-shot guard re-armed: a later trip fires again.
        assert await endpoints_repo.trip_breaker(db, endpoint_id) is True
        await db.commit()


async def test_concurrent_trips_single_true(factories):
    factory_a, factory_b = factories
    endpoint_id = await _seed_endpoint(factory_a, _endpoint_model())

    result_a, result_b = await _race(
        factory_a,
        factory_b,
        lambda db: endpoints_repo.trip_breaker(db, endpoint_id),
        lambda db: endpoints_repo.trip_breaker(db, endpoint_id),
    )

    # Exactly one True -> one WARN, one metric increment downstream (§5.5).
    assert sorted([result_a, result_b], key=bool) == [False, True]

    endpoint = await _get_endpoint(factory_a, endpoint_id)
    assert endpoint.enabled is False
    assert endpoint.disabled_reason == "circuit_breaker"


async def test_reenable_resets_breaker_state(session_factory):
    endpoint_id = await _seed_endpoint(
        session_factory,
        _endpoint_model(enabled=False, consecutive_failures=10, disabled_reason="circuit_breaker"),
    )

    async with session_factory() as db:
        endpoint = await endpoints_repo.get_endpoint(db, endpoint_id)
        assert endpoint is not None
        await endpoints_repo.reenable(db, endpoint)
        await db.commit()

    endpoint = await _get_endpoint(session_factory, endpoint_id)
    assert endpoint.enabled is True
    assert endpoint.consecutive_failures == 0
    assert endpoint.disabled_reason is None


async def test_redeliver_guarded_sql_reset(session_factory):
    async with session_factory() as db:
        endpoint = _endpoint_model()
        db.add(endpoint)
        await db.flush()
        failed = _delivery_model(
            endpoint.id,
            status="failed",
            attempts=4,
            response_code=500,
            last_error="ConnectTimeout",
        )
        pending = _delivery_model(endpoint.id)
        delivered = _delivery_model(endpoint.id, status="delivered", attempts=1, response_code=200)
        db.add_all([failed, pending, delivered])
        await db.commit()
        failed_id, pending_id, delivered_id = failed.id, pending.id, delivered.id

    async with session_factory() as db:
        assert await webhook_outbox.redeliver(db, failed_id) == failed_id
        await db.commit()

    async with session_factory() as db:
        row = await webhook_outbox.get_delivery(db, failed_id)
        assert row is not None
        assert row.status == "pending"
        assert row.attempts == 0
        assert row.response_code is None
        assert row.last_error is None
        db_now = (await db.execute(select(func.now()))).scalar_one()
        assert row.next_attempt_at <= db_now  # immediately due

        # Already pending -> None: the status predicate is load-bearing — a
        # Python status check would race the poller's in-flight claim (§4).
        assert await webhook_outbox.redeliver(db, pending_id) is None
        # Delivered rows qualify too (status IN ('delivered','failed')).
        assert await webhook_outbox.redeliver(db, delivered_id) == delivered_id
        # Unknown id -> None.
        assert await webhook_outbox.redeliver(db, uuid.uuid4()) is None
        await db.commit()
