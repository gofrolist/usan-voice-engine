"""Scheduler orchestrator — the third in-process poller (spec §5.1-§5.3).

This module currently holds the shared single-Call materializer (spec §5.3)
used by the poll cycle's schedule phase (3) and batch-target phase (4); the
poll loop itself (``poll_once``/``run_poller``, byte-for-byte the retry
orchestrator's loop discipline) ships with the loop half and is wired in
main.py lifespan as the third poller, after the retry and retention pollers.

Correctness rests on the spec §2.2 invariants, not on in-process state:
``scheduled_at IS NOT NULL`` marks a poller-owned row (retry child or
schedule/batch root — the existing claim/reclaim predicates already do the
right thing for both), and the deterministic ``sched:``/``batch:``
idempotency keys are the cross-replica/crash guard — a re-poll after a
partial crash, or a second replica, hits the unique key and takes the
verified-ownership replay path instead of dialing twice. Like the retry
orchestrator's count-then-claim gate and the outbound-trunk provisioning
cache in livekit_dispatch, anything racier than the key assumes the
documented single-replica deployment.

Dialing is NOT done here: materialized rows are QUEUED with ``scheduled_at``
set, and the existing retry poller claims and dials them when due —
inheriting the dial-time DNC and quiet-hours re-checks (spec §2.3).
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import schedule_windows
from usan_api.db.base import CallStatus
from usan_api.db.models import Call, Elder
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.settings import Settings


@dataclass(frozen=True)
class MaterializeOutcome:
    """Outcome of one materialization attempt (spec §5.3) — the caller maps
    ``result`` onto its bookkeeping (schedule ``last_result`` / target skip)."""

    result: str  # created | replayed | dnc_blocked | skipped_daily_cap | key_conflict
    call: Call | None


async def _replay_or_conflict(
    db: AsyncSession, elder: Elder, idempotency_key: str
) -> MaterializeOutcome:
    """Verified replay after a unique-key IntegrityError (spec §5.3 step 5).

    Adopt the existing row only when it is OURS: same elder and a chain root
    (``parent_call_id IS NULL``). Anything else is a squatted or foreign key —
    ERROR and refuse; never silently link a foreign call. Either way a key
    never dials twice.
    """
    existing = await calls_repo.get_by_idempotency_key(db, idempotency_key)
    if existing is not None and existing.elder_id == elder.id and existing.parent_call_id is None:
        return MaterializeOutcome("replayed", existing)
    logger.bind(elder_id=str(elder.id)).error(
        "Materialization key conflict: existing row is not ours; refusing to adopt"
    )
    return MaterializeOutcome("key_conflict", None)


async def materialize_call(
    db: AsyncSession,
    settings: Settings,
    *,
    elder: Elder,
    idempotency_key: str,
    scheduled_at: datetime,
    local_day: date,
    dynamic_vars: dict[str, Any],
    profile_override: uuid.UUID | None,
) -> MaterializeOutcome:
    """Materialize one autonomous root Call (spec §5.3, shared by phases 3 and 4).

    One Call per transaction — this function only flushes; the call insert and
    the caller's bookkeeping (schedule advance / target flip) commit atomically
    in the caller. Order: daily cap -> advisory phone lock -> DNC -> create; on
    IntegrityError (unique idempotency_key) SAVEPOINT-rollback (begin_nested),
    re-fetch and VERIFY OWNERSHIP (same elder, parent_call_id IS NULL) ->
    replayed, else key_conflict (ERROR log; never silently adopt a foreign row).
    """
    day_start, day_end = schedule_windows.day_bounds_utc(local_day, elder.timezone)
    roots = await calls_repo.count_autonomous_roots(
        db, elder_id=elder.id, day_start=day_start, day_end=day_end
    )
    if roots >= settings.max_autonomous_calls_per_elder_per_day:
        return MaterializeOutcome("skipped_daily_cap", None)

    # The same advisory lock the enqueue gate takes (one lock held at a time,
    # spec §5.2): serializes against concurrent add_dnc/enqueues for this number.
    await dnc_repo.lock_phone(db, elder.phone_e164)
    if await dnc_repo.is_blocked(db, elder.phone_e164):
        # Terminal DNC_BLOCKED row consuming the key — identical to enqueue_call's
        # gate; begin_nested so a key race here also takes the verified replay path.
        try:
            async with db.begin_nested():
                call = await calls_repo.create_materialized_root(
                    db,
                    elder_id=elder.id,
                    status=CallStatus.DNC_BLOCKED,
                    idempotency_key=idempotency_key,
                    scheduled_at=None,
                    dynamic_vars=dynamic_vars,
                    profile_override=profile_override,
                )
        except IntegrityError:
            return await _replay_or_conflict(db, elder, idempotency_key)
        return MaterializeOutcome("dnc_blocked", call)

    try:
        async with db.begin_nested():  # SAVEPOINT: a duplicate key rolls back here only
            call = await calls_repo.create_materialized_root(
                db,
                elder_id=elder.id,
                status=CallStatus.QUEUED,
                idempotency_key=idempotency_key,
                scheduled_at=scheduled_at,
                dynamic_vars=dynamic_vars,
                profile_override=profile_override,
            )
    except IntegrityError:
        return await _replay_or_conflict(db, elder, idempotency_key)
    return MaterializeOutcome("created", call)
