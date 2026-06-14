import uuid
from datetime import UTC, datetime, timedelta

import pytest
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api import retry_policy, webhook_events
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, Elder
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import call_batches as call_batches_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.batch import BatchTargetIn

FIXED_NOW = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)  # inside [09:00, 21:00) UTC


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(calls_repo, "_utcnow", lambda: FIXED_NOW)


async def _publish_policy_profile(db, *, policy=None):
    """Create a profile, optionally set a `policy` section, publish it; returns the id."""
    profile = await profiles_repo.create_profile(
        db, name=f"policy-{uuid.uuid4().hex}", description=None, actor_email="t@usan.test"
    )
    if policy is not None:
        cfg = dict(profile.draft_config)
        cfg["policy"] = policy
        await profiles_repo.update_draft(
            db, profile.id, config=cfg, description=None, actor_email="t@usan.test"
        )
    await profiles_repo.publish(db, profile.id, note=None, actor_email="t@usan.test")
    return profile.id


async def _seed_terminal(
    factory, *, status, attempt=1, timezone="UTC", dynamic_vars=None, policy=None
):
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone=timezone)
        if policy is not None:
            elder.agent_profile_id = await _publish_policy_profile(db, policy=policy)
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


async def _seed_batch_root(factory, *, root_status, cancel_batch, policy=None):
    """Seed a batch-owned chain root: batch + materialized target linked to a
    root call keyed ``batch:{id}:0`` (spec §5.6). Optionally cancel the batch
    AFTER the root exists — the cancel-vs-terminal-transition race window."""
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="B", phone_e164=phone, timezone="UTC")
        if policy is not None:
            elder.agent_profile_id = await _publish_policy_profile(db, policy=policy)
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
    # webhook_events' root walk powers origin attribution (spec §6.1): a bound
    # that lags the ceiling stops one hop short of the root at policy max depth,
    # parses the depth-1 child's NULL idempotency_key, and emits origin: null.
    assert webhook_events._MAX_CHAIN_HOPS == retry_policy.MAX_CHAIN_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_chain_root_origin_recovered_at_max_policy_depth(session_factory):
    # Origin attribution at the deepest policy-allowed chain (spec §6.1): the
    # tip sits MAX_CHAIN_ATTEMPTS - 1 = 4 hops from its batch: root, and
    # chain_root_origin must still reach the root and recover the batch origin
    # — a 3-hop walk would stop at the depth-1 child (no key) and emit
    # origin: null in the call.started/completed payloads.
    root_id, elder_id = await _seed_batch_root(
        session_factory, root_status=CallStatus.NO_ANSWER, cancel_batch=False
    )
    current_id = root_id
    for attempt in range(2, retry_policy.MAX_CHAIN_ATTEMPTS + 1):
        current_id = await _seed_child(
            session_factory,
            parent_id=current_id,
            elder_id=elder_id,
            status=CallStatus.NO_ANSWER,
            attempt=attempt,
        )
    async with session_factory() as db:
        tip = await calls_repo.get_call(db, current_id)
        assert tip is not None
        assert tip.attempt == retry_policy.MAX_CHAIN_ATTEMPTS
        origin = await webhook_events.chain_root_origin(db, tip)
    assert origin is not None, "origin must be recovered from the batch: root, not null"
    assert origin.source == "batch"
    assert origin.ordinal == 0


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


# --- Wiring site 1: schedule_retry resolves per-profile policy (spec §3.3.2) ---


@pytest.mark.asyncio
async def test_retry_delay_scaled_by_profile_multiplier(session_factory):
    # Elder's profile carries delay_multiplier=2.0: NO_ANSWER attempt 1 ladder
    # rung 30m scales to 60m. UTC elder at FIXED_NOW 12:00 -> 13:00 is inside
    # the window, so the clamp is exact.
    parent_id, _ = await _seed_terminal(
        session_factory,
        status=CallStatus.NO_ANSWER,
        attempt=1,
        policy={"retry_delay_multiplier": 2.0},
    )
    async with session_factory() as db:
        child = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert child is not None
    assert child.scheduled_at == FIXED_NOW + timedelta(minutes=60)


@pytest.mark.asyncio
async def test_retry_suppressed_by_max_attempts_zero(session_factory):
    # retry_max_attempts.busy=0 disables busy retries entirely: schedule_retry
    # returns None and never creates a child (chain-global semantics, §3.3.1).
    parent_id, _ = await _seed_terminal(
        session_factory,
        status=CallStatus.BUSY,
        attempt=1,
        policy={"retry_max_attempts": {"busy": 0}},
    )
    async with session_factory() as db:
        result = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert result is None
    assert await _child_count(session_factory, parent_id) == 0


@pytest.mark.asyncio
async def test_retry_clamped_to_narrowed_quiet_hours(session_factory):
    # Policy narrows the window start to 10:00 local. Halifax elder (ADT,
    # UTC-3 on 2026-05-31): FIXED_NOW 12:00 UTC == 09:00 local; FAILED's 1-min
    # ladder lands at 09:01 local — inside the STATUTORY window (no clamp
    # today) but before the policy start, so the child clamps to 10:00 local
    # == 13:00 UTC.
    parent_id, _ = await _seed_terminal(
        session_factory,
        status=CallStatus.FAILED,
        attempt=1,
        timezone="America/Halifax",
        policy={"quiet_hours_start_local": "10:00"},
    )
    async with session_factory() as db:
        child = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert child is not None
    assert child.scheduled_at == datetime(2026, 5, 31, 13, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_retry_honors_parent_override_policy_over_elder(session_factory):
    # Precedence threading pin: the parent's profile_override (multiplier 2.0)
    # outranks the elder's assigned profile (resolves, but carries no policy).
    # If only elder_profile_id were threaded, the walk would yield statutory
    # (+30m); the override must win (+60m) — whole-profile precedence, §3.3.2.
    parent_id, _ = await _seed_terminal(session_factory, status=CallStatus.NO_ANSWER, attempt=1)
    async with session_factory() as db:
        override_pid = await _publish_policy_profile(db, policy={"retry_delay_multiplier": 2.0})
        elder_pid = await _publish_policy_profile(db, policy=None)
        parent = await calls_repo.get_call(db, parent_id)
        parent.profile_override = override_pid
        elder = await db.get(Elder, parent.elder_id)
        elder.agent_profile_id = elder_pid
        await db.commit()
    async with session_factory() as db:
        child = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert child is not None
    assert child.scheduled_at == FIXED_NOW + timedelta(minutes=60)


@pytest.mark.asyncio
async def test_policy_reresolved_not_snapshotted(session_factory):
    # Re-resolve at consumption (spec §3.3.2): the parent exists BEFORE the
    # elder's profile publishes a tighter policy; schedule_retry must reflect
    # the new policy — never an enqueue-time snapshot.
    parent_id, elder_id = await _seed_terminal(
        session_factory, status=CallStatus.NO_ANSWER, attempt=1
    )
    async with session_factory() as db:
        pid = await _publish_policy_profile(db, policy=None)  # v1: no policy section
        elder = await db.get(Elder, elder_id)
        elder.agent_profile_id = pid
        await db.commit()
    async with session_factory() as db:
        profile = await profiles_repo.get_profile(db, pid)
        cfg = dict(profile.draft_config)
        cfg["policy"] = {"retry_delay_multiplier": 2.0}
        await profiles_repo.update_draft(
            db, pid, config=cfg, description=None, actor_email="t@usan.test"
        )
        await profiles_repo.publish(db, pid, note=None, actor_email="t@usan.test")
        await db.commit()
    async with session_factory() as db:
        child = await calls_repo.schedule_retry(db, parent_id)
        await db.commit()
    assert child is not None
    assert child.scheduled_at == FIXED_NOW + timedelta(minutes=60)


@pytest.mark.asyncio
async def test_schedule_retry_walk_finds_batch_root_at_max_depth(session_factory):
    # Jointly pins D4's derived walk bound AND the max_attempts threading: with
    # policy no_answer=4, an attempt-4 NO_ANSWER parent (3 hops from root)
    # passes the next_retry_delay guard — the builtin ladder would stop there,
    # BEFORE the root walk — so the walk must reach the batch: root and
    # suppress with the cancelled-batch log line. Asserting on the log pins
    # that the walk ran (a None for the wrong reason has no log).
    root_id, elder_id = await _seed_batch_root(
        session_factory,
        root_status=CallStatus.NO_ANSWER,
        cancel_batch=True,
        policy={"retry_max_attempts": {"no_answer": 4}},
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
    records: list[dict] = []
    handler_id = logger.add(lambda m: records.append(m.record), level="INFO")
    try:
        async with session_factory() as db:
            result = await calls_repo.schedule_retry(db, current_id)
            await db.commit()
    finally:
        logger.remove(handler_id)
    assert result is None
    assert await _child_count(session_factory, current_id) == 0
    suppressed = [r for r in records if r["message"] == "Retry suppressed: batch cancelled"]
    assert suppressed, "expected the batch-cancelled suppression log line (walk must run)"
    assert suppressed[0]["extra"].get("call_id") == str(current_id)
