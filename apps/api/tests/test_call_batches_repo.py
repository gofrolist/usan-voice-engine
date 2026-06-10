"""call_batches repository: guarded target lifecycle + throttled claim + drained-cancel."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import CallBatch, CallBatchTarget
from usan_api.repositories import call_batches as batches_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.batch import BatchTargetIn

NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    async with session_factory() as db:
        await db.execute(
            text("TRUNCATE call_batch_targets, call_batches, call_schedules, calls, elders CASCADE")
        )
        await db.commit()


async def _seed_elder(factory) -> uuid.UUID:
    phone = f"+1555{str(uuid.uuid4().int)[:7].zfill(7)}"
    async with factory() as db:
        elder = await elders_repo.create_elder(
            db, name="Batch Elder", phone_e164=phone, timezone="America/New_York"
        )
        await db.commit()
        return elder.id


async def _seed_call(factory, elder_id: uuid.UUID) -> uuid.UUID:
    async with factory() as db:
        call = await calls_repo.create_call(
            db,
            elder_id=elder_id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.QUEUED,
            livekit_room=f"usan-outbound-{uuid.uuid4()}",
        )
        await db.commit()
        return call.id


async def _create_batch(
    factory,
    *,
    name: str = "June campaign",
    idempotency_key: str | None = None,
    trigger_at: datetime | None = None,
    max_concurrency: int | None = None,
    n_targets: int = 3,
) -> tuple[uuid.UUID, list[uuid.UUID]]:
    elder_ids = [await _seed_elder(factory) for _ in range(n_targets)]
    async with factory() as db:
        batch = await batches_repo.create_batch_with_targets(
            db,
            name=name,
            idempotency_key=idempotency_key,
            payload_digest="d" * 64,
            trigger_at=trigger_at,
            window_start_local=None,
            window_end_local=None,
            days_of_week=None,
            max_concurrency=max_concurrency,
            profile_override=None,
            targets=[BatchTargetIn(elder_id=eid) for eid in elder_ids],
        )
        await db.commit()
        return batch.id, elder_ids


async def _set_batch(factory, batch_id: uuid.UUID, **values) -> None:
    async with factory() as db:
        await db.execute(update(CallBatch).where(CallBatch.id == batch_id).values(**values))
        await db.commit()


async def _set_target(factory, target_id: int, **values) -> None:
    async with factory() as db:
        await db.execute(
            update(CallBatchTarget).where(CallBatchTarget.id == target_id).values(**values)
        )
        await db.commit()


async def test_create_batch_with_targets_one_flush(session_factory):
    elder_ids = [await _seed_elder(session_factory) for _ in range(3)]
    async with session_factory() as db:
        batch = await batches_repo.create_batch_with_targets(
            db,
            name="June campaign",
            idempotency_key="batch-create-1",
            payload_digest="a" * 64,
            trigger_at=NOW + timedelta(hours=1),
            window_start_local=None,
            window_end_local=None,
            days_of_week=None,
            max_concurrency=2,
            profile_override=None,
            targets=[
                BatchTargetIn(elder_id=elder_ids[0], dynamic_vars={"first_name": "Rose"}),
                BatchTargetIn(elder_id=elder_ids[1]),
                BatchTargetIn(elder_id=elder_ids[2]),
            ],
        )
        await db.commit()
        batch_id = batch.id
        assert batch.status == "scheduled"
        assert batch.max_concurrency == 2
        assert batch.started_at is None

    async with session_factory() as db:
        targets = await batches_repo.list_targets(db, batch_id)
        assert [t.target_index for t in targets] == [0, 1, 2]
        assert all(t.status == "pending" for t in targets)
        assert [t.elder_id for t in targets] == elder_ids
        assert targets[0].dynamic_vars == {"first_name": "Rose"}
        assert all(t.call_id is None for t in targets)

    # uq_call_batch_targets_idx: a duplicate (batch_id, target_index) cannot exist.
    async with session_factory() as db:
        db.add(CallBatchTarget(batch_id=batch_id, target_index=1, elder_id=elder_ids[0]))
        with pytest.raises(IntegrityError):
            await db.flush()


async def test_get_by_idempotency_key(session_factory):
    batch_id, _ = await _create_batch(session_factory, idempotency_key="batch-key-1", n_targets=1)
    async with session_factory() as db:
        found = await batches_repo.get_by_idempotency_key(db, "batch-key-1")
        assert found is not None
        assert found.id == batch_id
        assert await batches_repo.get_by_idempotency_key(db, "batch-key-missing") is None
        fetched = await batches_repo.get_batch(db, batch_id)
        assert fetched is not None
        assert fetched.id == batch_id
        assert await batches_repo.get_batch(db, uuid.uuid4()) is None


async def test_trigger_due_batches_guarded(session_factory):
    b_due, _ = await _create_batch(
        session_factory, trigger_at=NOW - timedelta(minutes=5), n_targets=1
    )
    b_null, _ = await _create_batch(session_factory, trigger_at=None, n_targets=1)
    b_future, _ = await _create_batch(
        session_factory, trigger_at=NOW + timedelta(hours=1), n_targets=1
    )
    # Already-running row: never re-triggered (status guard), started_at untouched.
    b_running, _ = await _create_batch(
        session_factory, trigger_at=NOW - timedelta(minutes=5), n_targets=1
    )
    await _set_batch(session_factory, b_running, status="running")

    async with session_factory() as db:
        triggered = await batches_repo.trigger_due_batches(db, now=NOW, limit=10)
        assert {b.id for b in triggered} == {b_due, b_null}
        assert all(b.status == "running" and b.started_at == NOW for b in triggered)
        await db.commit()

    async with session_factory() as db:
        future = await batches_repo.get_batch(db, b_future)
        assert future is not None
        assert future.status == "scheduled"
        assert future.started_at is None
        running = await batches_repo.get_batch(db, b_running)
        assert running is not None
        assert running.started_at is None  # untouched by the trigger pass


async def test_claim_next_pending_target_order_and_throttle(session_factory):
    # --- order: targets of one running batch come back in target_index order ---
    batch_id, _ = await _create_batch(session_factory, n_targets=3)
    await _set_batch(session_factory, batch_id, status="running", started_at=NOW)

    claimed_order: list[int] = []
    for _ in range(3):
        async with session_factory() as db:
            target = await batches_repo.claim_next_pending_target(db)
            assert target is not None
            claimed_order.append(target.target_index)
            assert await batches_repo.mark_target_skipped(db, target, reason="daily_cap", now=NOW)
            await db.commit()
    assert claimed_order == [0, 1, 2]
    async with session_factory() as db:
        assert await batches_repo.claim_next_pending_target(db) is None  # drained

    # --- throttle: a batch at max_concurrency is passed over, others still served ---
    gated_id, gated_elders = await _create_batch(session_factory, max_concurrency=1, n_targets=2)
    await _set_batch(session_factory, gated_id, status="running", started_at=NOW)
    free_id, _ = await _create_batch(session_factory, n_targets=1)
    await _set_batch(session_factory, free_id, status="running", started_at=NOW)

    call_id = await _seed_call(session_factory, gated_elders[0])
    async with session_factory() as db:
        gated_targets = await batches_repo.list_targets(db, gated_id)
        assert await batches_repo.mark_target_materialized(
            db, gated_targets[0], call_id=call_id, now=NOW
        )
        await db.commit()

    # One unfinalized materialized target == max_concurrency=1: the gated batch's
    # remaining pending target must be passed over in favor of the free batch.
    async with session_factory() as db:
        target = await batches_repo.claim_next_pending_target(db)
        assert target is not None
        assert target.batch_id == free_id
        assert await batches_repo.mark_target_skipped(db, target, reason="daily_cap", now=NOW)
        await db.commit()

    async with session_factory() as db:
        assert await batches_repo.claim_next_pending_target(db) is None  # gated, free drained

    # Finalizing the materialized target releases the throttle.
    async with session_factory() as db:
        materialized = await batches_repo.list_materialized_targets(db, gated_id)
        assert [t.target_index for t in materialized] == [0]
        assert await batches_repo.finalize_target(
            db, materialized[0], final_status="completed", now=NOW
        )
        await db.commit()
    async with session_factory() as db:
        target = await batches_repo.claim_next_pending_target(db)
        assert target is not None
        assert target.batch_id == gated_id
        assert target.target_index == 1


async def test_claim_next_pending_target_skip_locked_under_open_txn(
    session_factory, async_database_url
):
    batch_id, _ = await _create_batch(session_factory, n_targets=2)
    await _set_batch(session_factory, batch_id, status="running", started_at=NOW)

    engine_b = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        factory_b = async_sessionmaker(engine_b, expire_on_commit=False)
        async with session_factory() as db_a:
            target_a = await batches_repo.claim_next_pending_target(db_a)
            assert target_a is not None
            assert target_a.target_index == 0  # A holds the row lock (txn open)
            async with factory_b() as db_b:
                target_b = await batches_repo.claim_next_pending_target(db_b)
                # B skips A's locked row instead of blocking (spec §9 SKIP LOCKED race).
                assert target_b is not None
                assert target_b.target_index == 1
            await db_a.rollback()
    finally:
        await engine_b.dispose()

    # A rolled back: target 0 is claimable again.
    async with session_factory() as db:
        target = await batches_repo.claim_next_pending_target(db)
        assert target is not None
        assert target.target_index == 0


async def test_target_transitions_are_status_guarded(session_factory):
    batch_id, elder_ids = await _create_batch(session_factory, n_targets=3)
    await _set_batch(session_factory, batch_id, status="running", started_at=NOW)
    call_id = await _seed_call(session_factory, elder_ids[0])

    async with session_factory() as db:
        targets = await batches_repo.list_targets(db, batch_id)
        cancelled, happy, skipped = targets
    await _set_target(session_factory, cancelled.id, status="cancelled")

    # materialize: pending only — a cancelled target is a no-op returning False.
    async with session_factory() as db:
        targets = await batches_repo.list_targets(db, batch_id)
        assert (
            await batches_repo.mark_target_materialized(db, targets[0], call_id=call_id, now=NOW)
            is False
        )
        await db.commit()
    async with session_factory() as db:
        targets = await batches_repo.list_targets(db, batch_id)
        assert targets[0].status == "cancelled"
        assert targets[0].call_id is None

    # happy path: pending -> materialized -> done; skip/finalize guards along the way.
    async with session_factory() as db:
        targets = await batches_repo.list_targets(db, batch_id)
        assert (
            await batches_repo.finalize_target(db, targets[1], final_status="completed", now=NOW)
            is False
        )  # finalize only from materialized
        assert await batches_repo.mark_target_materialized(db, targets[1], call_id=call_id, now=NOW)
        assert targets[1].status == "materialized"
        assert targets[1].call_id == call_id
        assert targets[1].materialized_at == NOW
        # skip only from pending — a materialized target refuses.
        assert (
            await batches_repo.mark_target_skipped(db, targets[1], reason="daily_cap", now=NOW)
            is False
        )
        assert await batches_repo.finalize_target(db, targets[1], final_status="completed", now=NOW)
        assert targets[1].status == "done"
        assert targets[1].final_status == "completed"
        assert targets[1].finalized_at == NOW
        # done is terminal: a second finalize refuses.
        assert (
            await batches_repo.finalize_target(db, targets[1], final_status="no_answer", now=NOW)
            is False
        )
        await db.commit()

    async with session_factory() as db:
        targets = await batches_repo.list_targets(db, batch_id)
        assert await batches_repo.mark_target_skipped(
            db, targets[2], reason="elder_deleted", now=NOW
        )
        assert targets[2].status == "skipped"
        assert targets[2].skip_reason == "elder_deleted"
        await db.commit()

    async with session_factory() as db:
        targets = await batches_repo.list_targets(db, batch_id)
        assert [t.status for t in targets] == ["cancelled", "done", "skipped"]


async def test_cancel_batch_marks_pending_cancelled_and_is_guarded(session_factory):
    batch_id, elder_ids = await _create_batch(session_factory, n_targets=3)
    await _set_batch(session_factory, batch_id, status="running", started_at=NOW)
    call_id = await _seed_call(session_factory, elder_ids[0])

    async with session_factory() as db:
        targets = await batches_repo.list_targets(db, batch_id)
        assert await batches_repo.mark_target_materialized(db, targets[0], call_id=call_id, now=NOW)
        await db.commit()

    async with session_factory() as db:
        batch = await batches_repo.get_batch(db, batch_id)
        assert batch is not None
        root_call_ids = await batches_repo.cancel_batch(db, batch, now=NOW)
        # materialized targets' root call ids, for the caller's guarded chain-tip cancel
        assert root_call_ids == [call_id]
        assert batch.status == "cancelled"
        assert batch.cancelled_at == NOW
        await db.commit()

    async with session_factory() as db:
        targets = await batches_repo.list_targets(db, batch_id)
        # pending -> cancelled; the materialized target is left for the finalizer.
        assert [t.status for t in targets] == ["materialized", "cancelled", "cancelled"]

    # Second cancel: idempotent no-op — batch unchanged, nothing returned.
    later = NOW + timedelta(minutes=5)
    async with session_factory() as db:
        batch = await batches_repo.get_batch(db, batch_id)
        assert batch is not None
        assert await batches_repo.cancel_batch(db, batch, now=later) == []
        assert batch.status == "cancelled"
        assert batch.cancelled_at == NOW  # not re-stamped
        await db.commit()

    # Completed batch refuses (the router maps ValueError to 409).
    done_id, _ = await _create_batch(session_factory, n_targets=1)
    await _set_batch(session_factory, done_id, status="completed", completed_at=NOW)
    async with session_factory() as db:
        batch = await batches_repo.get_batch(db, done_id)
        assert batch is not None
        with pytest.raises(ValueError, match="completed"):
            await batches_repo.cancel_batch(db, batch, now=NOW)


async def test_open_batches_and_complete_drained(session_factory):
    # Running batch with every target settled -> completed + completed_at.
    drained_id, _ = await _create_batch(session_factory, n_targets=3)
    await _set_batch(session_factory, drained_id, status="running", started_at=NOW)
    async with session_factory() as db:
        targets = await batches_repo.list_targets(db, drained_id)
    for target, status in zip(targets, ("done", "skipped", "cancelled"), strict=True):
        await _set_target(session_factory, target.id, status=status)

    # Running batch with an open (pending) target -> stays open, untouched.
    busy_id, _ = await _create_batch(session_factory, n_targets=1)
    await _set_batch(session_factory, busy_id, status="running", started_at=NOW)

    # Cancelled batch with zero open targets -> completed_at stamped, status STAYS
    # cancelled, and it leaves the open working set (idx_call_batches_open exit
    # condition — spec §9 drained-cancelled bookkeeping).
    cancelled_id, _ = await _create_batch(session_factory, n_targets=1)
    await _set_batch(session_factory, cancelled_id, status="cancelled", cancelled_at=NOW)
    async with session_factory() as db:
        targets = await batches_repo.list_targets(db, cancelled_id)
    await _set_target(session_factory, targets[0].id, status="cancelled")

    # Already-completed batch: never part of the open working set.
    closed_id, _ = await _create_batch(session_factory, n_targets=1)
    await _set_batch(session_factory, closed_id, status="completed", completed_at=NOW)

    async with session_factory() as db:
        open_before = await batches_repo.open_batches(db, limit=10)
        assert {b.id for b in open_before} == {drained_id, busy_id, cancelled_id}

    async with session_factory() as db:
        completed = await batches_repo.complete_drained_batches(db, now=NOW)
        assert {b.id for b in completed} == {drained_id, cancelled_id}
        await db.commit()

    async with session_factory() as db:
        drained = await batches_repo.get_batch(db, drained_id)
        assert drained is not None
        assert drained.status == "completed"
        assert drained.completed_at == NOW
        cancelled = await batches_repo.get_batch(db, cancelled_id)
        assert cancelled is not None
        assert cancelled.status == "cancelled"  # status survives the stamp
        assert cancelled.completed_at == NOW
        busy = await batches_repo.get_batch(db, busy_id)
        assert busy is not None
        assert busy.status == "running"
        assert busy.completed_at is None

        open_after = await batches_repo.open_batches(db, limit=10)
        assert [b.id for b in open_after] == [busy_id]


async def test_list_batches_clamped_and_ordered(session_factory):
    assert batches_repo.MAX_BATCHES_LIMIT == 500  # spec §8 bounded reads

    ids = [(await _create_batch(session_factory, n_targets=1))[0] for _ in range(3)]
    # Force identical created_at so ordering must fall back to the id tiebreaker.
    async with session_factory() as db:
        await db.execute(update(CallBatch).where(CallBatch.id.in_(ids)).values(created_at=NOW))
        await db.commit()
    await _set_batch(session_factory, ids[0], status="running", started_at=NOW)

    # Python UUID ordering matches Postgres uuid byte ordering, so id DESC is
    # computable client-side.
    expected = sorted(ids, reverse=True)
    async with session_factory() as db:
        rows = await batches_repo.list_batches(db)
        assert [b.id for b in rows] == expected  # newest-first, id tiebreaker

        running = await batches_repo.list_batches(db, status="running")
        assert [b.id for b in running] == [ids[0]]

        # limit clamps low (0 -> 1) and high (10_000 -> MAX_BATCHES_LIMIT);
        # offset pages past the first row.
        first_page = await batches_repo.list_batches(db, limit=0)
        assert [b.id for b in first_page] == expected[:1]
        rest = await batches_repo.list_batches(db, limit=10_000, offset=1)
        assert [b.id for b in rest] == expected[1:]


async def test_target_counts_and_histogram(session_factory):
    batch_id, _ = await _create_batch(session_factory, n_targets=7)
    async with session_factory() as db:
        targets = await batches_repo.list_targets(db, batch_id)
    plan = [
        ("pending", None),
        ("materialized", None),
        ("done", "completed"),
        ("done", "completed"),
        ("done", "no_answer"),
        ("skipped", None),
        ("cancelled", None),
    ]
    for target, (status, final_status) in zip(targets, plan, strict=True):
        await _set_target(session_factory, target.id, status=status, final_status=final_status)

    async with session_factory() as db:
        counts = await batches_repo.target_counts(db, batch_id)
        assert counts == {
            "pending": 1,
            "materialized": 1,
            "done": 3,
            "skipped": 1,
            "cancelled": 1,
        }
        histogram = await batches_repo.final_status_histogram(db, batch_id)
        assert histogram == {"completed": 2, "no_answer": 1}

    # A fresh batch zero-fills every status key (the operator progress view).
    fresh_id, _ = await _create_batch(session_factory, n_targets=1)
    async with session_factory() as db:
        counts = await batches_repo.target_counts(db, fresh_id)
        assert counts == {
            "pending": 1,
            "materialized": 0,
            "done": 0,
            "skipped": 0,
            "cancelled": 0,
        }
        assert await batches_repo.final_status_histogram(db, fresh_id) == {}
