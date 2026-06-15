"""Callback auto-dial poller (US8 / T075; FR-030/031).

A requested callback ("call me back in an hour") is logged by ``schedule_callback`` with a
best-effort parsed ``requested_at``. This poller turns a due request into an actual
outbound call: it claims the ``open`` request, clamps the dial time to the elder's quiet
hours, honors the DNC list, and materializes ONE root Call with a deterministic
``callback:{id}`` idempotency key — so a re-run never double-dials. The Call is then
dispatched by the existing scheduler/dispatch path; this module only materializes it.

The request advances ``open -> scheduled`` when the Call is created and ``scheduled ->
dialed`` once that Call has left the queue (reconcile pass). Ship-inert: wired only when
``callback_dialer_poller_enabled`` is set (main.py lifespan), exactly like the other
optional pollers.

Known limitation (spec "Evening + callback collision" edge case): this dialer does not
de-duplicate against a same-elder evening/scheduled call near the clamped time, so a
callback whose dial time overlaps the evening window can still produce two near-
simultaneous calls. Cross-schedule dedup is deferred (no US8 task); the per-elder
concurrency it would need belongs in the shared dispatch/concurrency layer, not here.
"""

import asyncio
import contextlib
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usan_api import quiet_hours
from usan_api.db.base import CallStatus
from usan_api.db.session import get_session_factory
from usan_api.repositories import callback_requests as callback_requests_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import dnc as dnc_repo
from usan_api.repositories import elders as elders_repo
from usan_api.settings import Settings

# Per-cycle ceiling on callbacks materialized; the rest wait for the next tick.
_MATERIALIZE_BUDGET = 100


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def poll_once(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """One callback-dial cycle: materialize due requests, then reconcile dialed state.

    ``now`` overrides the clock for deterministic tests. IDs are gathered once (a plain
    read) so a permanently-unmaterializable request is retried at most once per cycle, not
    spun on within one. Each request is materialized in its own transaction.
    """
    moment = now if now is not None else _utcnow()
    async with factory() as db:
        due_ids = await callback_requests_repo.list_due_open_ids(
            db, now=moment, limit=_MATERIALIZE_BUDGET
        )

    materialized = 0
    for request_id in due_ids:
        if await _materialize_one(factory, settings, request_id, moment):
            materialized += 1

    async with factory() as db:
        dialed = await callback_requests_repo.reconcile_dialed(db)
        await db.commit()

    return {"materialized": materialized, "dialed": dialed}


async def _materialize_one(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    request_id: int,
    moment: datetime,
) -> bool:
    """Materialize one due callback into a root Call. Returns True on materialization.

    Mirrors the scheduler's order (advisory phone lock -> DNC -> create under SAVEPOINT
    with verified key replay). A bad timezone fails CLOSED: the request is left ``open``
    (logged) rather than dialed outside the statutory window.
    """
    async with factory() as db:
        cb = await callback_requests_repo.claim_open_for_dial(db, request_id)
        if cb is None:
            return False  # another worker took it, or it is no longer open
        elder = await elders_repo.get_elder(db, cb.elder_id)
        if elder is None:
            logger.bind(callback_id=cb.id).warning("Callback elder missing; leaving open")
            return False
        if cb.requested_at is None:  # defensive: the due query already excludes these
            return False

        candidate = max(cb.requested_at, moment)
        try:
            scheduled_at = quiet_hours.next_allowed(candidate, elder.timezone)
        except ValueError:
            # Invalid IANA timezone — never dial outside quiet hours; a human resolves it.
            logger.bind(callback_id=cb.id).warning("Callback timezone invalid; leaving open")
            return False

        await dnc_repo.lock_phone(db, elder.phone_e164)
        blocked = await dnc_repo.is_blocked(db, elder.phone_e164)
        status = CallStatus.DNC_BLOCKED if blocked else CallStatus.QUEUED
        idempotency_key = f"callback:{cb.id}"
        try:
            async with db.begin_nested():  # SAVEPOINT: a duplicate key rolls back here only
                call = await calls_repo.create_materialized_root(
                    db,
                    elder_id=elder.id,
                    status=status,
                    idempotency_key=idempotency_key,
                    scheduled_at=None if blocked else scheduled_at,
                    dynamic_vars={},
                    profile_override=cb.profile_override,
                )
        except IntegrityError:
            # The key already exists (a prior cycle materialized it but did not mark the
            # callback). Adopt our own row rather than dialing twice.
            existing = await calls_repo.get_by_idempotency_key(db, idempotency_key)
            if existing is None or existing.elder_id != elder.id:
                logger.bind(callback_id=cb.id).error("Callback key conflict; refusing to adopt")
                return False
            call = existing

        await callback_requests_repo.mark_scheduled(db, cb.id, dispatched_call_id=call.id)
        await db.commit()
        logger.bind(callback_id=cb.id, call_id=str(call.id), blocked=blocked).info(
            "Materialized callback into outbound call"
        )
        return True


async def run_poller(settings: Settings, stop: asyncio.Event) -> None:
    """Background loop: one cycle every ``callback_dialer_poll_interval_s`` until stopped."""
    logger.bind(interval_s=settings.callback_dialer_poll_interval_s).info(
        "Callback dialer poller started"
    )
    factory = get_session_factory()
    while not stop.is_set():
        try:
            await poll_once(factory, settings)
        except Exception as exc:  # noqa: BLE001 - poller must survive; log TYPE only (PHI-safe)
            logger.bind(err=type(exc).__name__).error("callback dialer cycle failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.callback_dialer_poll_interval_s)
