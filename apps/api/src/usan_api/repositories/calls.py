import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from loguru import logger
from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import quiet_hours
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, CallBatch, CallBatchTarget, Elder
from usan_api.retry_policy import next_retry_delay


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def create_call(
    db: AsyncSession,
    *,
    elder_id: uuid.UUID,
    direction: CallDirection,
    status: CallStatus,
    idempotency_key: str | None = None,
    livekit_room: str | None = None,
    dynamic_vars: dict[str, Any] | None = None,
) -> Call:
    call = Call(
        elder_id=elder_id,
        direction=direction,
        status=status,
        idempotency_key=idempotency_key,
        livekit_room=livekit_room,
        dynamic_vars=dynamic_vars or {},
    )
    db.add(call)
    await db.flush()
    await db.refresh(call)
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
    call = await db.get(Call, call_id)
    if call is None:
        return None
    call.status = status
    if error is not None:
        call.error = error
    await db.flush()
    await db.refresh(call)
    return call


async def mark_answered(
    db: AsyncSession, call_id: uuid.UUID, *, sip_call_id: str | None
) -> Call | None:
    call = await db.get(Call, call_id)
    if call is None:
        return None
    call.status = CallStatus.IN_PROGRESS
    call.answered_at = _utcnow()
    if sip_call_id:
        call.sip_call_id = sip_call_id
    await db.flush()
    await db.refresh(call)
    return call


async def mark_dial_failure(
    db: AsyncSession,
    call_id: uuid.UUID,
    status: CallStatus,
    *,
    end_reason: str,
    error: dict[str, Any] | None = None,
) -> Call | None:
    call = await db.get(Call, call_id)
    if call is None:
        return None
    call.status = status
    call.ended_at = _utcnow()
    call.end_reason = end_reason
    if error is not None:
        call.error = error
    await db.flush()
    await db.refresh(call)
    return call


async def _latest_by_room(db: AsyncSession, livekit_room: str) -> Call | None:
    # Room names are uuid4 so a collision is astronomically unlikely; take the most
    # recent match rather than scalar_one_or_none(), which would 500 on a duplicate.
    result = await db.execute(
        select(Call)
        .where(Call.livekit_room == livekit_room)
        .order_by(Call.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def mark_completed_if_in_progress(db: AsyncSession, livekit_room: str) -> Call | None:
    call = await _latest_by_room(db, livekit_room)
    if call is None or call.status is not CallStatus.IN_PROGRESS:
        return None
    call.status = CallStatus.COMPLETED
    call.ended_at = _utcnow()
    call.end_reason = "hangup"
    if call.answered_at is not None:
        call.duration_seconds = int((call.ended_at - call.answered_at).total_seconds())
    await db.flush()
    await db.refresh(call)
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
    call = await db.get(Call, call_id)
    if call is None or call.status is not CallStatus.IN_PROGRESS:
        return None
    call.status = CallStatus.VOICEMAIL_LEFT
    call.ended_at = _utcnow()
    call.end_reason = "voicemail"
    if call.answered_at is not None:
        call.duration_seconds = int((call.ended_at - call.answered_at).total_seconds())
    await db.flush()
    await db.refresh(call)
    return call


async def schedule_retry(db: AsyncSession, call_id: uuid.UUID) -> Call | None:
    """Create the next-attempt child for a call that just reached a retryable
    terminal state (§5.3), in the caller's transaction.

    Returns the child, or None when: the policy says stop, the parent/elder is
    gone, the elder's timezone is invalid (fail CLOSED — never risk a TCPA-hour
    call), the chain's owning batch was cancelled (spec §5.6), or a retry child
    already exists (idempotent via the partial UNIQUE index on parent_call_id).
    """
    parent = await db.get(Call, call_id)
    if parent is None or parent.elder_id is None:
        return None
    delay = next_retry_delay(parent.status, parent.attempt)
    if delay is None:
        return None
    elder = await db.get(Elder, parent.elder_id)
    if elder is None:
        return None
    try:
        scheduled_at = quiet_hours.next_allowed(_utcnow() + delay, elder.timezone)
    except ValueError:
        logger.bind(call_id=str(call_id), timezone=elder.timezone).error(
            "Retry not scheduled: elder timezone is not a valid IANA zone"
        )
        return None

    # Batch-cancellation guard (spec §5.6): walk parent_call_id to the chain root
    # (<=3 hops); if the root is batch-owned and its batch is cancelled, never create
    # a child — in the SAME commit as the parent's terminal transition, so the
    # scheduler-cycle sweep is only a backstop for the cancel-vs-transition race.
    root = parent
    for _ in range(3):
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
        elder_id=parent.elder_id,
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


async def requeue_for_quiet_hours(
    db: AsyncSession, call_id: uuid.UUID, *, scheduled_at: datetime
) -> Call | None:
    """Flip a claimed DIALING row back to QUEUED with a fresh clamp (dial-time
    quiet-hours re-check, spec §2.3). Guarded on DIALING so it never clobbers an
    outcome written by a racing webhook."""
    call = await db.get(Call, call_id)
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
    elder_id: uuid.UUID | None,
    livekit_room: str,
    sip_call_id: str | None = None,
    dynamic_vars: dict[str, Any] | None = None,
) -> Call:
    """Create an answered inbound call (IN_PROGRESS, answered now).

    Inbound calls are answered by definition (the caller is on the line), so
    started_at/answered_at are set immediately; the room_finished webhook later
    marks COMPLETED and computes duration_seconds from answered_at. elder_id may
    be NULL for an unknown caller — the row still records the inbound attempt.
    """
    now = _utcnow()
    call = Call(
        elder_id=elder_id,
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
    return call


async def reclaim_stuck_dialing(
    db: AsyncSession, *, now: datetime, stale_after_s: int, limit: int
) -> list[uuid.UUID]:
    """Re-queue retry rows stranded in DIALING (ungraceful death mid-dispatch).

    A genuine in-flight dial leaves DIALING within the ring timeout, so a retry
    row still DIALING after ``stale_after_s`` (>> ring timeout) is stranded. Only
    retry rows (scheduled_at set) are reclaimed; a stranded initial call is the
    caller's to re-enqueue.
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

    Gated on IN_PROGRESS so it is idempotent and races the room_finished webhook
    safely: whichever marks the call COMPLETED first wins, the other no-ops.
    """
    call = await db.get(Call, call_id)
    if call is None or call.status is not CallStatus.IN_PROGRESS:
        return None
    call.status = CallStatus.COMPLETED
    call.ended_at = _utcnow()
    call.end_reason = end_reason
    if call.answered_at is not None:
        call.duration_seconds = int((call.ended_at - call.answered_at).total_seconds())
    await db.flush()
    await db.refresh(call)
    return call


async def mark_failed_if_active(
    db: AsyncSession, call_id: uuid.UUID, *, end_reason: str
) -> Call | None:
    """Transition a still-active call to FAILED. No-op (returns None) if the call
    already reached IN_PROGRESS or any terminal state, so a crash handler never
    clobbers a committed outcome.
    """
    call = await db.get(Call, call_id)
    if call is None or call.status not in _ACTIVE_STATUSES:
        return None
    call.status = CallStatus.FAILED
    call.ended_at = _utcnow()
    call.end_reason = end_reason
    await db.flush()
    await db.refresh(call)
    return call


# Retry ladders top out at 3 attempts (retry_policy.py), so a chain is root + 2
# children; one extra hop of headroom keeps the walk bounded, never load-bearing.
_MAX_CHAIN_HOPS = 3


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


async def cancel_queued_tips(db: AsyncSession, root_call_ids: Sequence[uuid.UUID]) -> int:
    """Guarded UPDATE: each chain's tip ``queued -> cancelled`` (the first writer
    of the CANCELLED enum value). Never touches ``dialing``/``in_progress`` rows —
    in-flight calls finish naturally (spec §5.6). Returns rows flipped."""
    tip_ids: list[uuid.UUID] = []
    for root_call_id in root_call_ids:
        tip = await get_chain_tip(db, root_call_id)
        if tip is not None:
            tip_ids.append(tip.id)
    if not tip_ids:
        return 0
    result = cast(
        "CursorResult[Any]",
        await db.execute(
            update(Call)
            .where(Call.id.in_(tip_ids), Call.status == CallStatus.QUEUED)
            .values(status=CallStatus.CANCELLED)
        ),
    )
    await db.flush()
    return int(result.rowcount or 0)
