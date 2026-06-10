"""In-process retry orchestrator (§4.1 RetryOrchestrator, §5.3 policy).

A single async loop per process. Each cycle: reap rows stranded in DIALING, count
in-flight dial-slot consumers, claim due rows with FOR UPDATE SKIP LOCKED (capped
by the free-slot budget when the concurrency gate is enabled), and dispatch each
as a tracked background task. Multiple replicas may each run this safely for
claiming — SKIP LOCKED and the partial UNIQUE index on parent_call_id make
claiming and scheduling idempotent. The concurrency gate's count-then-claim read,
however, is racy across replicas (each could observe the same free-slot budget,
overshooting the cap by up to one claim batch per extra replica); like the
outbound-trunk provisioning cache in livekit_dispatch, this assumes the documented
single-replica deployment.
"""

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usan_api import background, livekit_dispatch
from usan_api.db.session import get_session_factory
from usan_api.observability.custom_metrics import DIAL_SLOTS_FREE, IN_FLIGHT_CALLS
from usan_api.repositories import calls as calls_repo
from usan_api.settings import Settings


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def poll_once(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> list[uuid.UUID]:
    """One poll cycle: reap stranded DIALING rows, count in-flight, claim due rows,
    dispatch.

    Claimable rows are QUEUED with scheduled_at set — "poller-owned" rows: retry
    children and schedule/batch roots alike (spec §2.2 invariant 2; the
    idx_calls_due_retries index in migration 0003 serves exactly this predicate).

    Returns the claimed ids (at most ``retry_batch_size``, further capped by the
    free dial-slot budget when the concurrency gate is enabled — spec §5.4). The
    in-flight count and the claim share one transaction snapshot, so webhooks and
    ad-hoc dials cannot drift between them intra-process. The claim is committed
    before dispatch is spawned, so a spawned dispatch always sees DIALING.

    ``now`` overrides the clock used for the reclaim staleness cutoff, the
    in-flight recency bound, and the claim predicate (scheduled_at <= now). In
    production all steps use the same real-time instant; in tests a fixed value
    keeps the cycle deterministic.
    """
    moment = now if now is not None else _utcnow()
    async with factory() as db:
        await calls_repo.reclaim_stuck_dialing(
            db,
            now=moment,
            stale_after_s=settings.retry_stuck_dialing_s,
            limit=settings.retry_batch_size,
        )
        await db.commit()
    async with factory() as db:
        in_flight = await calls_repo.count_in_flight(
            db, now=moment, max_age_s=settings.outbound_max_call_duration_s + 120
        )
        free = max(0, settings.max_concurrent_calls - settings.reserved_concurrency - in_flight)
        # Export the gauges every cycle, in all flag states (spec §5.4(2)/§7):
        # they live here — not in the scheduler, which may be disabled while the
        # gate is live — so the dial-slot picture is truthful pre-enable too.
        IN_FLIGHT_CALLS.set(in_flight)
        DIAL_SLOTS_FREE.set(free)
        claimed: list[uuid.UUID]
        if settings.autonomous_dialing_paused:
            logger.bind(component="retry_poller").warning(
                "Autonomous dialing paused; claiming nothing this cycle"
            )
            claimed = []
        elif settings.concurrency_gate_enabled:
            limit = min(settings.retry_batch_size, free)
            claimed = (
                await calls_repo.claim_due_retries(db, now=moment, limit=limit) if limit > 0 else []
            )
        else:
            claimed = await calls_repo.claim_due_retries(
                db, now=moment, limit=settings.retry_batch_size
            )
        await db.commit()
    async with factory() as db:
        missing = await calls_repo.reconcile_missing_recordings(
            db,
            now=moment,
            grace_s=settings.recording_reconcile_grace_s,
            limit=settings.retry_batch_size,
        )
        await db.commit()
    for cid in missing:
        logger.bind(call_id=str(cid)).warning(
            "Recording missing: egress started but reported no result within the grace "
            "window; recording_uri stays NULL"
        )
    for call_id in claimed:
        background.spawn(livekit_dispatch.dispatch_and_dial(call_id, settings))
    return claimed


async def run_poller(settings: Settings, stop: asyncio.Event) -> None:
    """Loop poll_once on the configured interval until ``stop`` is set.

    Survives per-cycle exceptions (logged, never fatal). The interval sleep is a
    cancellable wait on ``stop``, so shutdown is prompt.
    """
    log = logger.bind(component="retry_poller")
    log.info("Retry poller started (interval={i}s)", i=settings.retry_poll_interval_s)
    factory = get_session_factory()
    while not stop.is_set():
        try:
            claimed = await poll_once(factory, settings)
            if claimed:
                log.info("Dispatched {n} due retry call(s)", n=len(claimed))
        except Exception:
            log.opt(exception=True).error("Retry poll cycle failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.retry_poll_interval_s)
    log.info("Retry poller stopped")
