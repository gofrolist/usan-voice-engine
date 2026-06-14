"""``batch.completed`` enqueued at phase-6 drain settlement (Task C6).

Pins spec §6.6's single-emission-point contract: phase-6
``_complete_drained_batches`` is the ONLY place a ``batch.completed`` event is
enqueued — both drained-``completed`` and drained-``cancelled`` batches emit,
the stamp + enqueue share ONE transaction (commit or roll back together, the
§2.1 transactional outbox), the ``completed_at`` stamp makes re-runs
exactly-once, and the cancel endpoint enqueues nothing (its
``cancel_queued_tips`` already emits per-call events through the C4 mutator).
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import schedule_orchestrator, webhook_events
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, CallBatch, CallBatchTarget, WebhookDelivery, WebhookEndpoint
from usan_api.repositories import call_batches as batches_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.batch import BatchTargetIn

NOW = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    # Wraps the test (before AND after): leftover webhook_endpoints rows would
    # change fan-out counts in unrelated modules (test_webhook_enqueue_calls
    # precedent), and leftover OPEN batches would pollute the phase-6 working
    # set here (it stamps every drained batch it can see).
    async def _wipe():
        async with session_factory() as db:
            await db.execute(
                text(
                    "TRUNCATE webhook_deliveries, webhook_endpoints, "
                    "call_batch_targets, call_batches, calls, elders CASCADE"
                )
            )
            await db.commit()

    await _wipe()
    yield
    await _wipe()


@pytest.fixture
async def endpoint(session_factory) -> uuid.UUID:
    """One enabled endpoint subscribed to ``batch.completed`` only."""
    async with session_factory() as db:
        ep = WebhookEndpoint(
            url="https://hooks.example.com/sink",
            secret="a" * 64,
            events=["batch.completed"],
        )
        db.add(ep)
        await db.commit()
        return ep.id


async def _seed_settled_batch(
    factory,
    *,
    status: str = "running",
    final_statuses: tuple[str, ...] = ("completed",),
) -> uuid.UUID:
    """One batch forced to ``status`` with every target settled ``done`` —
    drained, so phase 6 stamps it on the next run."""
    elder_ids = []
    for _ in final_statuses:
        phone = f"+1555{str(uuid.uuid4().int)[:7]}"
        async with factory() as db:
            elder = await elders_repo.create_elder(db, name="S", phone_e164=phone, timezone="UTC")
            await db.commit()
            elder_ids.append(elder.id)
    async with factory() as db:
        batch = await batches_repo.create_batch_with_targets(
            db,
            name="campaign",
            idempotency_key=None,
            payload_digest="d" * 64,
            trigger_at=None,
            window_start_local=None,
            window_end_local=None,
            days_of_week=None,
            max_concurrency=None,
            profile_override=None,
            targets=[BatchTargetIn(elder_id=eid) for eid in elder_ids],
        )
        batch.status = status
        batch.started_at = NOW
        if status == "cancelled":
            batch.cancelled_at = NOW
        await db.commit()
        batch_id = batch.id
    async with factory() as db:
        targets = await batches_repo.list_targets(db, batch_id)
        for target, final_status in zip(targets, final_statuses, strict=True):
            await db.execute(
                update(CallBatchTarget)
                .where(CallBatchTarget.id == target.id)
                .values(status="done", final_status=final_status, finalized_at=NOW)
            )
        await db.commit()
    return batch_id


async def _batch_events(factory) -> list[WebhookDelivery]:
    async with factory() as db:
        result = await db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.event == "batch.completed")
            .order_by(WebhookDelivery.created_at)
        )
        return list(result.scalars().all())


async def _get_batch(factory, batch_id: uuid.UUID) -> CallBatch:
    async with factory() as db:
        batch = await db.get(CallBatch, batch_id)
        assert batch is not None
        return batch


async def test_drained_running_batch_emits_completed(session_factory, endpoint):
    batch_id = await _seed_settled_batch(
        session_factory, final_statuses=("completed", "completed", "no_answer")
    )

    stamped = await schedule_orchestrator._complete_drained_batches(session_factory, now=NOW)

    assert stamped == 1
    rows = await _batch_events(session_factory)
    assert len(rows) == 1
    row = rows[0]
    assert (row.endpoint_id, row.status, row.attempts) == (endpoint, "pending", 0)
    assert row.payload["event"] == "batch.completed"
    data = row.payload["data"]
    assert data["batch_id"] == str(batch_id)
    assert data["status"] == "completed"
    assert data["completed_at"] is not None
    assert data["target_count"] == 3
    assert data["final_status_histogram"] == {"completed": 2, "no_answer": 1}


async def test_drained_cancelled_batch_emits_cancelled_status(session_factory, endpoint):
    # BOTH drained statuses emit (spec §6.6) — a cancelled batch's event
    # arrives when its in-flight work settles, with status preserved.
    batch_id = await _seed_settled_batch(
        session_factory, status="cancelled", final_statuses=("cancelled", "no_answer")
    )

    stamped = await schedule_orchestrator._complete_drained_batches(session_factory, now=NOW)

    assert stamped == 1
    rows = await _batch_events(session_factory)
    assert len(rows) == 1
    data = rows[0].payload["data"]
    assert data["batch_id"] == str(batch_id)
    assert data["status"] == "cancelled"
    assert data["completed_at"] is not None
    assert data["final_status_histogram"] == {"cancelled": 1, "no_answer": 1}


async def test_phase6_rerun_emits_nothing_new(session_factory, endpoint):
    await _seed_settled_batch(session_factory)

    first = await schedule_orchestrator._complete_drained_batches(session_factory, now=NOW)
    second = await schedule_orchestrator._complete_drained_batches(
        session_factory, now=NOW + timedelta(minutes=5)
    )

    # The completed_at stamp removes the batch from the open set — exactly-once.
    assert (first, second) == (1, 0)
    assert len(await _batch_events(session_factory)) == 1


async def test_batch_event_same_txn_pre_commit_invisible(
    session_factory, endpoint, monkeypatch, async_database_url
):
    # Same-txn atomicity, not mere co-occurrence: probe from a FRESH engine at
    # payload-build time — the stamp and the enqueue must be invisible until
    # the single phase-6 commit makes both durable together.
    batch_id = await _seed_settled_batch(session_factory)
    real_builder = webhook_events.batch_completed_payload
    probes: list[tuple[int, datetime | None]] = []

    async def _probing(db: AsyncSession, batch: CallBatch) -> dict[str, Any]:
        payload = await real_builder(db, batch)
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as fresh:
                count = (
                    await fresh.execute(
                        select(func.count())
                        .select_from(WebhookDelivery)
                        .where(WebhookDelivery.event == "batch.completed")
                    )
                ).scalar_one()
                fresh_batch = await fresh.get(CallBatch, batch.id)
                assert fresh_batch is not None
                probes.append((int(count), fresh_batch.completed_at))
        finally:
            await engine.dispose()
        return payload

    monkeypatch.setattr(webhook_events, "batch_completed_payload", _probing)

    stamped = await schedule_orchestrator._complete_drained_batches(session_factory, now=NOW)

    assert stamped == 1
    # At probe time the stamp + enqueue were uncommitted: a fresh session saw
    # neither the delivery row nor the completed_at stamp.
    assert probes == [(0, None)]
    assert (await _get_batch(session_factory, batch_id)).completed_at == NOW
    assert len(await _batch_events(session_factory)) == 1


async def test_batch_event_failure_rolls_back_stamp_and_event(
    session_factory, endpoint, monkeypatch
):
    # Stamp and event commit or roll back TOGETHER: an implementation that
    # commits the stamp in a separate transaction fails here.
    batch_id = await _seed_settled_batch(session_factory)

    async def _boom(db: AsyncSession, batch: CallBatch) -> dict[str, Any]:
        raise RuntimeError("boom")

    monkeypatch.setattr(webhook_events, "batch_completed_payload", _boom)

    with pytest.raises(RuntimeError, match="boom"):
        await schedule_orchestrator._complete_drained_batches(session_factory, now=NOW)

    batch = await _get_batch(session_factory, batch_id)
    assert batch.completed_at is None
    assert batch.status == "running"
    assert await _batch_events(session_factory) == []


def test_cancel_endpoint_emits_no_batch_event(client, operator_headers, async_database_url):
    # The cancel endpoint enqueues NOTHING itself (spec §2.1/§10.7): the
    # batch-level event arrives only at drain settlement, and the idempotent
    # re-cancel cannot double-emit.
    phone = f"+1555{str(uuid.uuid4().int)[:7].zfill(7)}"
    r = client.post(
        "/v1/elders",
        json={"name": "Rose Elder", "phone_e164": phone, "timezone": "America/New_York"},
        headers=operator_headers,
    )
    assert r.status_code == 201
    elder_id = r.json()["id"]
    r = client.post(
        "/v1/batches",
        json={"name": "June campaign", "targets": [{"elder_id": elder_id}]},
        headers=operator_headers,
    )
    assert r.status_code == 201
    batch_id = r.json()["id"]

    async def _seed_running_with_inflight(db: AsyncSession) -> None:
        db.add(
            WebhookEndpoint(
                url="https://hooks.example.com/sink",
                secret="a" * 64,
                events=["batch.completed"],
            )
        )
        await db.execute(
            update(CallBatch)
            .where(CallBatch.id == uuid.UUID(batch_id))
            .values(status="running", started_at=NOW)
        )
        call = Call(
            elder_id=uuid.UUID(elder_id),
            direction=CallDirection.OUTBOUND,
            status=CallStatus.IN_PROGRESS,
            livekit_room=f"usan-outbound-{uuid.uuid4()}",
        )
        db.add(call)
        await db.flush()
        targets = await batches_repo.list_targets(db, uuid.UUID(batch_id))
        assert await batches_repo.mark_target_materialized(db, targets[0], call_id=call.id, now=NOW)

    async def _count_batch_events(db: AsyncSession) -> int:
        result = await db.execute(
            select(func.count())
            .select_from(WebhookDelivery)
            .where(WebhookDelivery.event == "batch.completed")
        )
        return int(result.scalar_one())

    async def _run_db(fn) -> Any:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                result = await fn(db)
                await db.commit()
                return result
        finally:
            await engine.dispose()

    asyncio.run(_run_db(_seed_running_with_inflight))

    cancel = client.post(f"/v1/batches/{batch_id}/cancel", headers=operator_headers)
    assert cancel.status_code == 200
    assert asyncio.run(_run_db(_count_batch_events)) == 0
    # Idempotent re-cancel: still zero — the endpoint cannot double-emit.
    recancel = client.post(f"/v1/batches/{batch_id}/cancel", headers=operator_headers)
    assert recancel.status_code == 200
    assert asyncio.run(_run_db(_count_batch_events)) == 0
