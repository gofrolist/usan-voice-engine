import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import retry_policy
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import call_batches as call_batches_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.batch import BatchTargetIn

FIXED_NOW = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)  # inside [09:00, 21:00) UTC


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(calls_repo, "_utcnow", lambda: FIXED_NOW)


async def _seed_terminal(factory, *, status, attempt=1, timezone="UTC", dynamic_vars=None):
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone=timezone)
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=status,
            dynamic_vars=dynamic_vars or {},
            livekit_room="usan-outbound-parent",
        )
        # create_call defaults attempt via the model; set explicitly for the test
        call.attempt = attempt
        await db.flush()
        await db.commit()
        return call.id, elder.id


async def _child_count(factory, parent_id) -> int:
    async with factory() as db:
        result = await db.execute(
            select(func.count()).select_from(Call).where(Call.parent_call_id == parent_id)
        )
        return result.scalar_one()


@pytest.mark.asyncio
async def test_schedule_retry_creates_child(session_factory):
    parent_id, elder_id = await _seed_terminal(
        session_factory, status=CallStatus.NO_ANSWER, attempt=1, dynamic_vars={"k": "v"}
    )
    async with session_factory() as db:
        child = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert child is not None
    # re-read in a fresh session to prove it persisted
    async with session_factory() as db:
        reloaded = await calls_repo.get_call(db, child.id)
    assert reloaded is not None
    assert reloaded.parent_call_id == parent_id
    assert reloaded.attempt == 2
    assert reloaded.status is CallStatus.QUEUED
    assert reloaded.elder_id == elder_id
    assert reloaded.dynamic_vars == {"k": "v"}
    assert reloaded.idempotency_key is None
    assert reloaded.livekit_room.startswith("usan-outbound-")
    assert reloaded.livekit_room != "usan-outbound-parent"
    assert reloaded.scheduled_at is not None
    assert reloaded.scheduled_at.tzinfo is not None
    # no_answer attempt 1 -> +30min, inside the UTC window -> exact
    assert reloaded.scheduled_at == FIXED_NOW + timedelta(minutes=30)


@pytest.mark.asyncio
async def test_schedule_retry_is_idempotent(session_factory):
    parent_id, _ = await _seed_terminal(session_factory, status=CallStatus.BUSY, attempt=1)
    async with session_factory() as db:
        first = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    async with session_factory() as db:
        second = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert first is not None
    assert second is None
    assert await _child_count(session_factory, parent_id) == 1


@pytest.mark.asyncio
async def test_schedule_retry_stops_at_policy_cap(session_factory):
    parent_id, _ = await _seed_terminal(session_factory, status=CallStatus.NO_ANSWER, attempt=3)
    async with session_factory() as db:
        result = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert result is None
    assert await _child_count(session_factory, parent_id) == 0


@pytest.mark.asyncio
async def test_schedule_retry_noop_for_non_retryable_status(session_factory):
    parent_id, _ = await _seed_terminal(session_factory, status=CallStatus.COMPLETED, attempt=1)
    async with session_factory() as db:
        result = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert result is None
    assert await _child_count(session_factory, parent_id) == 0


@pytest.mark.asyncio
async def test_schedule_retry_none_when_elder_missing(session_factory):
    # elder_id is ON DELETE SET NULL, so a parent can legitimately have no elder.
    parent_id, _ = await _seed_terminal(session_factory, status=CallStatus.FAILED, attempt=1)
    async with session_factory() as db:
        parent = await calls_repo.get_call(db, parent_id)
        parent.elder_id = None
        await db.commit()
    async with session_factory() as db:
        result = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert result is None
    assert await _child_count(session_factory, parent_id) == 0


@pytest.mark.asyncio
async def test_schedule_retry_none_for_missing_parent(session_factory):
    async with session_factory() as db:
        assert await calls_repo.schedule_retry(db, uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_schedule_retry_fails_closed_on_bad_timezone(session_factory):
    parent_id, _ = await _seed_terminal(
        session_factory, status=CallStatus.NO_ANSWER, attempt=1, timezone="Not/AZone"
    )
    async with session_factory() as db:
        result = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert result is None
    assert await _child_count(session_factory, parent_id) == 0


@pytest.mark.asyncio
async def test_schedule_retry_child_inherits_profile_override(session_factory):
    # profile_override is live (runtime agent-config + SMS template resolution),
    # so attempts 2..n must keep it instead of silently reverting to the default
    # profile (spec §2.3(3)/§6.1).
    parent_id, _ = await _seed_terminal(
        session_factory, status=CallStatus.NO_ANSWER, attempt=1, dynamic_vars={"k": "v"}
    )
    async with session_factory() as db:
        profile = await profiles_repo.create_profile(
            db, name=f"retry-override-{uuid.uuid4()}", description=None, actor_email="t@usan.test"
        )
        await profiles_repo.publish(db, profile.id, note=None, actor_email="t@usan.test")
        parent = await calls_repo.get_call(db, parent_id)
        parent.profile_override = profile.id
        await db.commit()
        profile_id = profile.id
    async with session_factory() as db:
        child = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert child is not None
    # re-read in a fresh session to prove it persisted
    async with session_factory() as db:
        reloaded = await calls_repo.get_call(db, child.id)
    assert reloaded is not None
    assert reloaded.profile_override == profile_id
    # regression: dynamic_vars still copied alongside the override
    assert reloaded.dynamic_vars == {"k": "v"}


async def _seed_batch_root(factory, *, root_status, cancel_batch):
    """Seed a batch-owned chain root: batch + materialized target linked to a
    root call keyed ``batch:{id}:0`` (spec §5.6). Optionally cancel the batch
    AFTER the root exists — the cancel-vs-terminal-transition race window."""
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="B", phone_e164=phone, timezone="UTC")
        batch = await call_batches_repo.create_batch_with_targets(
            db,
            name="d3-batch",
            idempotency_key=None,
            payload_digest=f"d3-{uuid.uuid4()}",
            trigger_at=None,
            window_start_local=None,
            window_end_local=None,
            days_of_week=None,
            max_concurrency=None,
            profile_override=None,
            targets=[BatchTargetIn(elder_id=elder.id)],
        )
        batch.status = "running"
        root = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=root_status,
            idempotency_key=f"batch:{batch.id}:0",
            livekit_room="usan-outbound-root",
        )
        targets = await call_batches_repo.list_targets(db, batch.id)
        assert await call_batches_repo.mark_target_materialized(
            db, targets[0], call_id=root.id, now=FIXED_NOW
        )
        if cancel_batch:
            await call_batches_repo.cancel_batch(db, batch, now=FIXED_NOW)
        await db.commit()
        return root.id, elder.id


async def _seed_child(factory, *, parent_id, elder_id, status, attempt):
    """Append a retry-chain child (parent_call_id linked list, spec §6.2)."""
    async with factory() as db:
        child = await calls_repo.create_call(
            db,
            elder_id=elder_id,
            direction=CallDirection.OUTBOUND,
            status=status,
            livekit_room=f"usan-outbound-{uuid.uuid4()}",
        )
        child.parent_call_id = parent_id
        child.attempt = attempt
        await db.flush()
        await db.commit()
        return child.id


@pytest.mark.asyncio
async def test_schedule_retry_suppressed_for_cancelled_batch_chain(session_factory):
    # The §5.6/§9 race the scheduler sweep alone would lose: FAILED children are
    # born at +1m and the retry poller claims every 30s, so the guard must live in
    # the same commit as the parent's terminal transition — i.e. in schedule_retry.
    root_id, _ = await _seed_batch_root(
        session_factory, root_status=CallStatus.DIALING, cancel_batch=True
    )
    async with session_factory() as db:
        # The in-flight call terminates AFTER the batch was cancelled.
        await calls_repo.mark_dial_failure(db, root_id, CallStatus.FAILED, end_reason="dial_failed")
        result = await calls_repo.schedule_retry(db, root_id)
        await db.commit()
    assert result is None
    assert await _child_count(session_factory, root_id) == 0


@pytest.mark.asyncio
async def test_schedule_retry_suppressed_for_grandchild_of_cancelled_batch(session_factory):
    # The guard walks parent_call_id to the chain root (<=3 hops); a cancelled
    # batch suppresses attempt 3 even though only the ROOT carries the batch key.
    # NO_ANSWER at attempt 2 normally retries (+2h) — FAILED would stop by policy
    # alone and never exercise the guard.
    root_id, elder_id = await _seed_batch_root(
        session_factory, root_status=CallStatus.NO_ANSWER, cancel_batch=True
    )
    child_id = await _seed_child(
        session_factory,
        parent_id=root_id,
        elder_id=elder_id,
        status=CallStatus.NO_ANSWER,
        attempt=2,
    )
    async with session_factory() as db:
        result = await calls_repo.schedule_retry(db, child_id)
        await db.commit()
    assert result is None
    assert await _child_count(session_factory, child_id) == 0


@pytest.mark.asyncio
async def test_schedule_retry_unaffected_for_running_batch_and_sched_roots(session_factory):
    # Control: the guard must not over-suppress. A running batch's chain retries
    # normally, and sched:-rooted chains are exempt from the batch check (§5.6).
    running_root_id, _ = await _seed_batch_root(
        session_factory, root_status=CallStatus.FAILED, cancel_batch=False
    )
    async with session_factory() as db:
        child = await calls_repo.schedule_retry(db, running_root_id)
        await db.commit()
    assert child is not None
    assert await _child_count(session_factory, running_root_id) == 1

    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with session_factory() as db:
        elder = await elders_repo.create_elder(db, name="S", phone_e164=phone, timezone="UTC")
        sched_root = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.FAILED,
            idempotency_key=f"sched:{uuid.uuid4()}:2026-06-10",
            livekit_room="usan-outbound-sched-root",
        )
        await db.commit()
        sched_root_id = sched_root.id
    async with session_factory() as db:
        sched_child = await calls_repo.schedule_retry(db, sched_root_id)
        await db.commit()
    assert sched_child is not None
    assert await _child_count(session_factory, sched_root_id) == 1


@pytest.mark.asyncio
async def test_schedule_retry_clamps_into_quiet_hours(session_factory):
    # Eastern elder; FIXED_NOW 12:00 UTC == 08:00 EDT (before 09:00 EDT).
    # voicemail_left attempt 1 -> +3h == 15:00 UTC == 11:00 EDT (now inside window) -> exact.
    parent_id, _ = await _seed_terminal(
        session_factory,
        status=CallStatus.VOICEMAIL_LEFT,
        attempt=1,
        timezone="America/New_York",
    )
    async with session_factory() as db:
        child = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert child is not None
    assert child.scheduled_at == FIXED_NOW + timedelta(hours=3)


def test_chain_hops_derive_from_policy_ceiling():
    # The chain-walk bound DERIVES from the policy ceiling (spec §3.3.1): a
    # chain is root + 4 retries (the RetryMaxAttempts le=4 ceiling), so the
    # deepest tip sits MAX_CHAIN_ATTEMPTS - 1 hops from its root. A literal
    # that lags the ceiling reintroduces the chain-tip escape (a depth-4 tip
    # invisible to get_chain_tip / cancel_queued_tips).
    assert calls_repo._MAX_CHAIN_HOPS == retry_policy.MAX_CHAIN_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_get_chain_tip_reaches_depth_four_tip(session_factory):
    # Root + 4 children (attempts 1..5): a policy max_attempts=4 chain's tip
    # sits 4 hops from root — the walk must reach it, not stop one short.
    root_id, elder_id = await _seed_terminal(
        session_factory, status=CallStatus.NO_ANSWER, attempt=1
    )
    current_id = root_id
    for attempt in range(2, 6):
        current_id = await _seed_child(
            session_factory,
            parent_id=current_id,
            elder_id=elder_id,
            status=CallStatus.NO_ANSWER,
            attempt=attempt,
        )
    async with session_factory() as db:
        tip = await calls_repo.get_chain_tip(db, root_id)
    assert tip is not None
    assert tip.id == current_id
    assert tip.attempt == 5


@pytest.mark.asyncio
async def test_batch_cancel_flips_max_depth_tip(session_factory):
    # The escape the invariant exists to kill (spec §3.3.1): a cancelled batch's
    # chain grown to depth 4 (policy-extended retries) still has its QUEUED tip
    # found and flipped — a 3-hop walk would stop at the depth-3 parent, leave
    # the tip QUEUED, and the cancelled batch would dial anyway.
    root_id, elder_id = await _seed_batch_root(
        session_factory, root_status=CallStatus.NO_ANSWER, cancel_batch=True
    )
    current_id = root_id
    for attempt in range(2, 5):
        current_id = await _seed_child(
            session_factory,
            parent_id=current_id,
            elder_id=elder_id,
            status=CallStatus.NO_ANSWER,
            attempt=attempt,
        )
    tip_id = await _seed_child(
        session_factory,
        parent_id=current_id,
        elder_id=elder_id,
        status=CallStatus.QUEUED,
        attempt=5,
    )
    async with session_factory() as db:
        flipped = await calls_repo.cancel_queued_tips(db, [root_id])
        await db.commit()
    assert flipped == 1
    async with session_factory() as db:
        tip = await calls_repo.get_call(db, tip_id)
    assert tip is not None
    assert tip.status is CallStatus.CANCELLED
