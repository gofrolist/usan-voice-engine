import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import notifications, quiet_hours, webhook_events
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, CallBatch, CallBatchTarget, Contact
from usan_api.repositories import agent_profiles, webhook_outbox
from usan_api.repositories import follow_up_flags as follow_up_flags_repo
from usan_api.retry_policy import MAX_CHAIN_ATTEMPTS, next_retry_delay
from usan_api.schemas.call import RESERVED_KEY_PREFIXES  # single source (spec §2.2 invariant 3)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# Every status a call can never leave. Guarded mutators re-check against this
# under the row lock; C4's set_status enqueue gate (non-terminal -> terminal)
# consumes it too (spec §2.1).
_TERMINAL_STATUSES = frozenset(
    {
        CallStatus.COMPLETED,
        CallStatus.VOICEMAIL_LEFT,
        CallStatus.NO_ANSWER,
        CallStatus.BUSY,
        CallStatus.FAILED,
        CallStatus.DNC_BLOCKED,
        CallStatus.CANCELLED,
    }
)


async def _enqueue_call_completed(db: AsyncSession, call: Call) -> None:
    """Fan call.completed into the transactional outbox INSIDE the caller's
    transaction, after the guarded transition's flush (spec §2.1): business
    change and event commit or roll back together; the guard's no-op paths
    never reach this, so one occurrence produces exactly one enqueue."""
    await webhook_outbox.enqueue_event(
        db, event="call.completed", payload=await webhook_events.call_completed_payload(db, call)
    )


async def _enqueue_call_started(db: AsyncSession, call: Call) -> None:
    """call.started twin of _enqueue_call_completed (spec §2.1 table)."""
    await webhook_outbox.enqueue_event(
        db, event="call.started", payload=await webhook_events.call_started_payload(db, call)
    )


async def create_call(
    db: AsyncSession,
    *,
    contact_id: uuid.UUID,
    direction: CallDirection,
    status: CallStatus,
    idempotency_key: str | None = None,
    livekit_room: str | None = None,
    dynamic_vars: dict[str, Any] | None = None,
    profile_override: uuid.UUID | None = None,
) -> Call:
    call = Call(
        contact_id=contact_id,
        direction=direction,
        status=status,
        idempotency_key=idempotency_key,
        livekit_room=livekit_room,
        dynamic_vars=dynamic_vars or {},
        profile_override=profile_override,
    )
    db.add(call)
    await db.flush()
    await db.refresh(call)
    if status is CallStatus.DNC_BLOCKED:
        # Terminal at birth (spec §2.1): the DNC gate refuses to dial, so this
        # row never passes through a terminal mutator — emit here or never.
        await _enqueue_call_completed(db, call)
    return call


async def get_call(db: AsyncSession, call_id: uuid.UUID) -> Call | None:
    return await db.get(Call, call_id)


async def get_by_idempotency_key(db: AsyncSession, key: str) -> Call | None:
    result = await db.execute(select(Call).where(Call.idempotency_key == key))
    return result.scalar_one_or_none()


async def set_status(
    db: AsyncSession,
    call_id: uuid.UUID,
    status: CallStatus,
    *,
    error: dict[str, Any] | None = None,
) -> Call | None:
    call = await db.get(Call, call_id, with_for_update=True)
    if call is None:
        return None
    # Captured under the row lock, before assignment: the call.completed enqueue
    # is gated on the non-terminal -> terminal crossing only (spec §2.1), so the
    # dial-time DNC_BLOCKED and dispatch-failure FAILED call sites emit with zero
    # call-site edits while terminal -> terminal rewrites enqueue nothing.
    old_status = call.status
    call.status = status
    if error is not None:
        call.error = error
    await db.flush()
    await db.refresh(call)
    if old_status not in _TERMINAL_STATUSES and status in _TERMINAL_STATUSES:
        await _enqueue_call_completed(db, call)
    return call


async def mark_answered(
    db: AsyncSession, call_id: uuid.UUID, *, sip_call_id: str | None
) -> Call | None:
    """Guarded transition to IN_PROGRESS (spec §2.1): the WHOLE write — not just
    the status — is gated on a pre-answer status under the row lock, so a late
    answer event can never resurrect a room_finished-completed call back to
    IN_PROGRESS and pin an in-flight slot (the pre-existing zombie bug). RINGING
    is never assigned today; kept as dead-state tolerance.
    """
    call = await db.get(Call, call_id, with_for_update=True)
    if call is None or call.status not in (CallStatus.DIALING, CallStatus.RINGING):
        return None
    call.status = CallStatus.IN_PROGRESS
    call.answered_at = _utcnow()
    if sip_call_id:
        call.sip_call_id = sip_call_id
    await db.flush()
    await db.refresh(call)
    await _enqueue_call_started(db, call)
    return call


async def mark_dial_failure(
    db: AsyncSession,
    call_id: uuid.UUID,
    status: CallStatus,
    *,
    end_reason: str,
    error: dict[str, Any] | None = None,
) -> Call | None:
    """Guarded dial-failure transition (spec §2.1): DIALING-only — deliberately
    narrower than §2.1's "(queued/dialing)" prose, because §10.7 pins "stale
    mark_dial_failure after a reclaim_stuck_dialing re-queue is a no-op" (a
    QUEUED match would clobber the fresh re-queued attempt) and every caller
    (livekit_dispatch's dispatch_and_dial / _dial_and_classify paths) operates
    on a row it claimed into DIALING. Already-terminal rows return None
    (callers verified tolerant).
    """
    call = await db.get(Call, call_id, with_for_update=True)
    if call is None or call.status is not CallStatus.DIALING:
        return None
    call.status = status
    call.ended_at = _utcnow()
    call.end_reason = end_reason
    if error is not None:
        call.error = error
    await db.flush()
    await db.refresh(call)
    await _enqueue_call_completed(db, call)
    return call


async def _latest_by_room(
    db: AsyncSession, livekit_room: str, *, for_update: bool = False
) -> Call | None:
    # Room names are uuid4 so a collision is astronomically unlikely; take the most
    # recent match rather than scalar_one_or_none(), which would 500 on a duplicate.
    # for_update=True locks the row so a guarded mutator's status check holds (§2.1).
    stmt = (
        select(Call)
        .where(Call.livekit_room == livekit_room)
        .order_by(Call.created_at.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalars().first()


async def mark_completed_if_in_progress(db: AsyncSession, livekit_room: str) -> Call | None:
    """Guarded transition (spec §2.1): the row lock makes the IN_PROGRESS check
    atomic, so the loser of a webhook-vs-outcome race re-reads the terminal
    status and returns None instead of double-transitioning."""
    call = await _latest_by_room(db, livekit_room, for_update=True)
    if call is None or call.status is not CallStatus.IN_PROGRESS:
        return None
    call.status = CallStatus.COMPLETED
    call.ended_at = _utcnow()
    call.end_reason = "hangup"
    if call.answered_at is not None:
        call.duration_seconds = int((call.ended_at - call.answered_at).total_seconds())
    await db.flush()
    await db.refresh(call)
    await _enqueue_call_completed(db, call)
    return call


async def set_egress_id(db: AsyncSession, livekit_room: str, egress_id: str) -> Call | None:
    call = await _latest_by_room(db, livekit_room)
    if call is None:
        return None
    call.egress_id = egress_id
    await db.flush()
    await db.refresh(call)
    return call


async def set_recording_uri(db: AsyncSession, livekit_room: str, recording_uri: str) -> Call | None:
    call = await _latest_by_room(db, livekit_room)
    if call is None:
        return None
    call.recording_uri = recording_uri
    call.recording_status = "complete"
    await db.flush()
    await db.refresh(call)
    return call


async def set_recording_status(db: AsyncSession, livekit_room: str, status: str) -> Call | None:
    call = await _latest_by_room(db, livekit_room)
    if call is None:
        return None
    call.recording_status = status
    await db.flush()
    await db.refresh(call)
    return call


async def mark_voicemail_left_if_in_progress(db: AsyncSession, call_id: uuid.UUID) -> Call | None:
    """Guarded transition (spec §2.1): IN_PROGRESS check held under the row lock —
    the loser of the voicemail-vs-room_finished race returns None."""
    call = await db.get(Call, call_id, with_for_update=True)
    if call is None or call.status is not CallStatus.IN_PROGRESS:
        return None
    call.status = CallStatus.VOICEMAIL_LEFT
    call.ended_at = _utcnow()
    call.end_reason = "voicemail"
    if call.answered_at is not None:
        call.duration_seconds = int((call.ended_at - call.answered_at).total_seconds())
    await db.flush()
    await db.refresh(call)
    await _enqueue_call_completed(db, call)
    return call


async def schedule_retry(db: AsyncSession, call_id: uuid.UUID) -> Call | None:
    """Create the next-attempt child for a call that just reached a retryable
    terminal state (§5.3), in the caller's transaction.

    Returns the child, or None when: the policy says stop, the parent/contact is
    gone, the contact's timezone is invalid (fail CLOSED — never risk a TCPA-hour
    call), the chain's owning batch was cancelled (spec §5.6), or a retry child
    already exists (idempotent via the partial UNIQUE index on parent_call_id).
    """
    parent = await db.get(Call, call_id)
    if parent is None or parent.contact_id is None:
        return None
    contact = await db.get(Contact, parent.contact_id)
    if contact is None:
        return None
    # Policy is re-resolved at EVERY consumption site, never snapshotted onto
    # the Call (TCPA dial-time truth, spec §3.3.2) — a compliance-tightening
    # publish must bind retries of already-terminal parents. The resolve needs
    # contact.agent_profile_id, so the delay computation moved after the contact
    # load; the early-return order is otherwise preserved (parent/contact missing
    # -> None, then delay-None -> None, then tz clamp, then batch-root walk).
    policy = await agent_profiles.resolve_call_policy(
        db,
        profile_override=parent.profile_override,
        contact_profile_id=contact.agent_profile_id,
        direction="outbound",
    )
    delay = next_retry_delay(
        parent.status,
        parent.attempt,
        max_attempts=policy.max_attempts_for(parent.status),
        delay_multiplier=policy.delay_multiplier,
    )
    if delay is None:
        # Retry policy exhausted → this call is terminally MISSED (FR-010). Alert the
        # family contacts opted in to missed-call alerts; if none is registered, surface
        # the miss to the operator queue (FR-013 / T088). Idempotent on the call id, so a
        # finalizer re-entry never double-notifies. Joins the caller's transaction.
        dispatch = await notifications.dispatch_family_alert(
            db, contact_id=contact.id, reason="missed_call", dedupe_base=f"missed:{parent.id}"
        )
        if not dispatch.had_contacts:
            await follow_up_flags_repo.ensure_operator_missed_flag(
                db, call_id=parent.id, contact_id=contact.id
            )
        return None
    try:
        scheduled_at = quiet_hours.next_allowed(
            _utcnow() + delay,
            contact.timezone,
            start_local=policy.start_local,
            end_local=policy.end_local,
        )
    except ValueError:
        logger.bind(call_id=str(call_id), timezone=contact.timezone).error(
            "Retry not scheduled: contact timezone is not a valid IANA zone"
        )
        return None

    # Batch-cancellation guard (spec §5.6): walk parent_call_id to the chain root
    # (<=_MAX_CHAIN_HOPS hops); if the root is batch-owned and its batch is cancelled,
    # never create a child — in the SAME commit as the parent's terminal transition,
    # so the scheduler-cycle sweep is only a backstop for the cancel-vs-transition
    # race. Deriving this bound is consistency, not a bug fix: under le=4 the deepest
    # parent that can still schedule a retry is attempt 4 = 3 hops from root, which
    # range(3) already reached (spec §3.3.1 overstates this site); the load-bearing
    # bounds are get_chain_tip/cancel_queued_tips, and deriving all three from
    # MAX_CHAIN_ATTEMPTS keeps them from drifting if the le=4 ceiling ever rises.
    root = parent
    for _ in range(_MAX_CHAIN_HOPS):
        if root.parent_call_id is None:
            break
        nxt = await db.get(Call, root.parent_call_id)
        if nxt is None:
            break
        root = nxt
    if root.idempotency_key and root.idempotency_key.startswith("batch:"):
        result = await db.execute(
            select(CallBatch.status)
            .join(CallBatchTarget, CallBatchTarget.batch_id == CallBatch.id)
            .where(CallBatchTarget.call_id == root.id)  # idx_call_batch_targets_call
        )
        if result.scalar_one_or_none() == "cancelled":
            logger.bind(call_id=str(call_id)).info("Retry suppressed: batch cancelled")
            return None

    child = Call(
        contact_id=parent.contact_id,
        direction=CallDirection.OUTBOUND,
        status=CallStatus.QUEUED,
        dynamic_vars=dict(parent.dynamic_vars),
        profile_override=parent.profile_override,
        parent_call_id=parent.id,
        attempt=parent.attempt + 1,
        scheduled_at=scheduled_at,
        livekit_room=f"usan-outbound-{uuid.uuid4()}",
    )
    try:
        async with db.begin_nested():  # SAVEPOINT: a duplicate child rolls back here only
            db.add(child)
            await db.flush()
    except IntegrityError:
        return None  # a sibling attempt already scheduled this retry
    await db.refresh(child)
    return child


async def claim_due_retries(db: AsyncSession, *, now: datetime, limit: int) -> list[uuid.UUID]:
    """Lock and claim up to ``limit`` due retry rows (QUEUED with a past
    scheduled_at), flipping each to DIALING. FOR UPDATE SKIP LOCKED lets multiple
    pollers run without claiming the same row.

    Returns AT MOST ``limit`` ids, and possibly fewer under concurrency (other
    pollers may hold locks on earlier-ordered rows) — never treat an under-full
    batch as "no more work".
    """
    result = await db.execute(
        select(Call)
        .where(
            Call.status == CallStatus.QUEUED,
            Call.scheduled_at.is_not(None),
            Call.scheduled_at <= now,
        )
        .order_by(Call.scheduled_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    claimed = list(result.scalars().all())
    for call in claimed:
        call.status = CallStatus.DIALING
    await db.flush()
    return [call.id for call in claimed]


async def count_in_flight(db: AsyncSession, *, now: datetime, max_age_s: int) -> int:
    """Recency-bounded count of dial-slot consumers (served by idx_calls_in_flight).

    LiveKit enforces max_call_duration on every outbound dial, so any in-flight row
    older than that ceiling is wedged (lost room_finished webhook or agent end-call
    — only DIALING has a reaper today) and must not consume a slot forever: without
    the bound, max_concurrent_calls wedged rows would silently and permanently halt
    ALL autonomous dialing (spec §5.4).
    """
    cutoff = now - timedelta(seconds=max_age_s)
    result = await db.execute(
        select(func.count())
        .select_from(Call)
        .where(
            Call.status.in_((CallStatus.DIALING, CallStatus.RINGING, CallStatus.IN_PROGRESS)),
            Call.updated_at > cutoff,
        )
    )
    return int(result.scalar_one())


async def count_queued_due(db: AsyncSession, *, now: datetime) -> int:
    """COUNT(*) of due QUEUED rows — exactly the idx_calls_due_retries predicate
    (status='queued' AND scheduled_at <= now, mirroring claim_due_retries); feeds
    the scheduler's batch-materialization slot math (spec §5.2 phase 4)."""
    result = await db.execute(
        select(func.count())
        .select_from(Call)
        .where(
            Call.status == CallStatus.QUEUED,
            Call.scheduled_at.is_not(None),
            Call.scheduled_at <= now,
        )
    )
    return int(result.scalar_one())


async def count_autonomous_roots(
    db: AsyncSession, *, contact_id: uuid.UUID, day_start: datetime, day_end: datetime
) -> int:
    """Roots with a reserved-prefix key whose scheduled_at falls inside the contact-local
    day bounds (daily repetition cap, spec §5.3 step 1; served by idx_calls_contact).

    The LIKE patterns derive from RESERVED_KEY_PREFIXES — only materializer-owned
    roots count toward the cap; retry children carry no key and ad-hoc calls carry
    no reserved prefix, so neither eats the contact's daily autonomous budget.
    """
    result = await db.execute(
        select(func.count())
        .select_from(Call)
        .where(
            Call.contact_id == contact_id,
            or_(*(Call.idempotency_key.like(f"{prefix}%") for prefix in RESERVED_KEY_PREFIXES)),
            Call.scheduled_at >= day_start,
            Call.scheduled_at < day_end,
        )
    )
    return int(result.scalar_one())


async def create_materialized_root(
    db: AsyncSession,
    *,
    contact_id: uuid.UUID,
    status: CallStatus,
    idempotency_key: str,
    scheduled_at: datetime | None,
    dynamic_vars: dict[str, Any],
    profile_override: uuid.UUID | None,
) -> Call:
    """Insert one materializer-owned chain root (spec §5.3 step 4) in the caller's
    transaction (flush only — the caller commits call + bookkeeping atomically).

    A livekit_room is minted only for QUEUED rows; DNC_BLOCKED rows mirror
    enqueue_call's gate (terminal, no room, no scheduled_at — the deterministic
    key is still consumed). Raises IntegrityError on a duplicate idempotency_key:
    callers SAVEPOINT-wrap and take the verified replay path (§5.3 step 5).
    """
    call = Call(
        contact_id=contact_id,
        direction=CallDirection.OUTBOUND,
        status=status,
        idempotency_key=idempotency_key,
        scheduled_at=scheduled_at,
        dynamic_vars=dict(dynamic_vars),
        profile_override=profile_override,
        attempt=1,
        livekit_room=f"usan-outbound-{uuid.uuid4()}" if status is CallStatus.QUEUED else None,
    )
    db.add(call)
    await db.flush()
    await db.refresh(call)
    if status is CallStatus.DNC_BLOCKED:
        # Terminal at birth, same as create_call's DNC gate (spec §2.1 table).
        await _enqueue_call_completed(db, call)
    return call


async def requeue_for_quiet_hours(
    db: AsyncSession, call_id: uuid.UUID, *, scheduled_at: datetime
) -> Call | None:
    """Flip a claimed DIALING row back to QUEUED with a fresh clamp (dial-time
    quiet-hours re-check, spec §2.3). Guarded on DIALING UNDER THE ROW LOCK
    (the §2.1 hardening, same as mark_dial_failure) so it never clobbers — or
    resurrects — a terminal outcome committed by a racing webhook."""
    call = await db.get(Call, call_id, with_for_update=True)
    if call is None or call.status is not CallStatus.DIALING:
        return None
    call.status = CallStatus.QUEUED
    call.scheduled_at = scheduled_at
    await db.flush()
    await db.refresh(call)
    return call


async def create_inbound_call(
    db: AsyncSession,
    *,
    contact_id: uuid.UUID | None,
    livekit_room: str,
    sip_call_id: str | None = None,
    dynamic_vars: dict[str, Any] | None = None,
) -> Call:
    """Create an answered inbound call (IN_PROGRESS, answered now).

    Inbound calls are answered by definition (the caller is on the line), so
    started_at/answered_at are set immediately; the room_finished webhook later
    marks COMPLETED and computes duration_seconds from answered_at. contact_id may
    be NULL for an unknown caller — the row still records the inbound attempt.
    """
    now = _utcnow()
    call = Call(
        contact_id=contact_id,
        direction=CallDirection.INBOUND,
        status=CallStatus.IN_PROGRESS,
        livekit_room=livekit_room,
        sip_call_id=sip_call_id,
        dynamic_vars=dynamic_vars or {},
        started_at=now,
        answered_at=now,
    )
    db.add(call)
    await db.flush()
    await db.refresh(call)
    # Inbound calls are answered at birth (spec §2.1 table) — call.started here.
    await _enqueue_call_started(db, call)
    return call


async def reclaim_stuck_dialing(
    db: AsyncSession, *, now: datetime, stale_after_s: int, limit: int
) -> list[uuid.UUID]:
    """Re-queue poller-owned rows stranded in DIALING (ungraceful death mid-dispatch).

    A genuine in-flight dial leaves DIALING within the ring timeout, so a row
    still DIALING after ``stale_after_s`` (>> ring timeout) is stranded. Only
    poller-owned rows (scheduled_at set — retry children and schedule/batch roots
    alike, spec §2.2 invariant 2) are reclaimed; a stranded ad-hoc initial call is
    the caller's to re-enqueue.
    """
    cutoff = now - timedelta(seconds=stale_after_s)
    result = await db.execute(
        select(Call)
        .where(
            Call.status == CallStatus.DIALING,
            Call.scheduled_at.is_not(None),
            Call.updated_at < cutoff,
        )
        .order_by(Call.updated_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    stuck = list(result.scalars().all())
    for call in stuck:
        call.status = CallStatus.QUEUED
    await db.flush()
    return [call.id for call in stuck]


async def reconcile_missing_recordings(
    db: AsyncSession, *, now: datetime, grace_s: int, limit: int
) -> list[uuid.UUID]:
    """Flag ended calls whose egress started but never reported a result.

    A call with an egress_id but no recording_uri and no recording_status, whose room
    ended more than ``grace_s`` ago, never received an egress_ended webhook (egress
    crashed or the delivery was lost). Mark recording_status='missing' so the gap is
    visible and not re-flagged (spec §8); recording_uri stays NULL and the call is
    otherwise complete. A late webhook can still recover it — it correlates by room,
    independent of status. SKIP LOCKED lets multiple pollers run safely.
    """
    cutoff = now - timedelta(seconds=grace_s)
    result = await db.execute(
        select(Call)
        .where(
            Call.egress_id.is_not(None),
            Call.recording_uri.is_(None),
            Call.recording_status.is_(None),
            Call.ended_at.is_not(None),
            Call.ended_at < cutoff,
        )
        .order_by(Call.ended_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    stranded = list(result.scalars().all())
    for call in stranded:
        call.recording_status = "missing"
    await db.flush()
    return [call.id for call in stranded]


_ACTIVE_STATUSES = frozenset({CallStatus.QUEUED, CallStatus.DIALING, CallStatus.RINGING})


async def complete_call_if_in_progress(
    db: AsyncSession, call_id: uuid.UUID, *, end_reason: str
) -> Call | None:
    """Mark an in-progress call COMPLETED with a caller-supplied end_reason.

    Gated on IN_PROGRESS under the row lock (spec §2.1) so it is idempotent and
    races the room_finished webhook safely: whichever marks the call COMPLETED
    first wins, the other re-reads the terminal status and no-ops.
    """
    call = await db.get(Call, call_id, with_for_update=True)
    if call is None or call.status is not CallStatus.IN_PROGRESS:
        return None
    call.status = CallStatus.COMPLETED
    call.ended_at = _utcnow()
    call.end_reason = end_reason
    if call.answered_at is not None:
        call.duration_seconds = int((call.ended_at - call.answered_at).total_seconds())
    await db.flush()
    await db.refresh(call)
    await _enqueue_call_completed(db, call)
    return call


async def mark_failed_if_active(
    db: AsyncSession, call_id: uuid.UUID, *, end_reason: str
) -> Call | None:
    """Transition a still-active call to FAILED. No-op (returns None) if the call
    already reached IN_PROGRESS or any terminal state, so a crash handler never
    clobbers a committed outcome. The check holds under the row lock (spec §2.1),
    so it cannot race a concurrent mark_dial_failure into a double transition.
    """
    call = await db.get(Call, call_id, with_for_update=True)
    if call is None or call.status not in _ACTIVE_STATUSES:
        return None
    call.status = CallStatus.FAILED
    call.ended_at = _utcnow()
    call.end_reason = end_reason
    await db.flush()
    await db.refresh(call)
    await _enqueue_call_completed(db, call)
    return call


# A chain is at most MAX_CHAIN_ATTEMPTS calls — root + 4 retries, the
# RetryMaxAttempts le=4 ceiling (spec §3.3.1) — so the deepest tip sits
# MAX_CHAIN_ATTEMPTS - 1 hops from its root. Derived, never a literal: a bound
# that lags the ceiling makes a depth-4 tip invisible to get_chain_tip and
# uncancellable by cancel_queued_tips (the chain-tip escape).
_MAX_CHAIN_HOPS = MAX_CHAIN_ATTEMPTS - 1


async def get_chain_tip(db: AsyncSession, root_call_id: uuid.UUID) -> Call | None:
    """Latest attempt of a retry chain: follow child rows via ``parent_call_id``
    (<=_MAX_CHAIN_HOPS hops; one-child-max via ``uq_calls_parent_call_id`` makes
    each probe a single indexed lookup)."""
    current = await db.get(Call, root_call_id)
    if current is None:
        return None
    for _ in range(_MAX_CHAIN_HOPS):
        result = await db.execute(select(Call).where(Call.parent_call_id == current.id))
        child = result.scalar_one_or_none()
        if child is None:
            break
        current = child
    return current


async def has_child(db: AsyncSession, call_id: uuid.UUID) -> bool:
    """True when a retry child row exists for ``call_id`` — a single indexed
    probe via ``uq_calls_parent_call_id``; the finalizer's §6.2 "no child" test
    (after a terminal transition's commit, either the child exists or it never
    will, so this probe is race-free within one snapshot)."""
    result = await db.execute(select(Call.id).where(Call.parent_call_id == call_id).limit(1))
    return result.scalar_one_or_none() is not None


async def cancel_queued_tips(db: AsyncSession, root_call_ids: Sequence[uuid.UUID]) -> int:
    """Guarded UPDATE: each chain's tip ``queued -> cancelled`` (the first writer
    of the CANCELLED enum value). Never touches ``dialing``/``in_progress`` rows —
    in-flight calls finish naturally (spec §5.6). Returns rows flipped (the
    ``-> int`` signature is kept for both callers, executor note 4); RETURNING
    feeds one call.completed{status=cancelled} per flipped row into the outbox
    (spec §2.1), with populate_existing so the tips get_chain_tip already loaded
    into this session are refreshed rather than served stale."""
    tip_ids: list[uuid.UUID] = []
    for root_call_id in root_call_ids:
        tip = await get_chain_tip(db, root_call_id)
        if tip is not None:
            tip_ids.append(tip.id)
    if not tip_ids:
        return 0
    result = await db.execute(
        update(Call)
        .where(Call.id.in_(tip_ids), Call.status == CallStatus.QUEUED)
        .values(status=CallStatus.CANCELLED)
        .returning(Call)
        .execution_options(populate_existing=True)
    )
    cancelled = list(result.scalars().all())
    for call in cancelled:
        await _enqueue_call_completed(db, call)
    await db.flush()
    return len(cancelled)
