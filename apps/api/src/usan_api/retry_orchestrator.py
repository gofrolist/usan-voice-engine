"""In-process retry orchestrator (§4.1 RetryOrchestrator, §5.3 policy).

A single async loop per process. Each cycle: reap rows stranded in DIALING, claim
due retry rows with FOR UPDATE SKIP LOCKED, and dispatch each as a tracked
background task. Multiple replicas may each run this safely — SKIP LOCKED and the
partial UNIQUE index on parent_call_id make claiming and scheduling idempotent.
"""

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usan_api import background, livekit_dispatch
from usan_api.db.session import get_session_factory
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
    """One poll cycle: reap stranded DIALING rows, claim due retries, dispatch.

    Returns the claimed ids (at most ``retry_batch_size``). The claim is committed
    before dispatch is spawned, so a spawned dispatch always sees DIALING.

    ``now`` overrides the clock used for both the reclaim staleness cutoff and the
    claim predicate (scheduled_at <= now). In production both steps use the same
    real-time instant; in tests a fixed value keeps the cycle deterministic.
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
