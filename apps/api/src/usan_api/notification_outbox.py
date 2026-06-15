"""Notification outbox poller (Clara Care Parity 002, foundational).

Delivers the non-call notifications enqueued by notifications.py — family alerts
(crisis/missed-call), monthly reports, opt-out acks — i.e. ``sms_messages`` rows with
``call_id IS NULL``. The in-call counterpart (sms_outbox.py) flushes ``call_id``-bound
rows post-call; this poller is its always-running sibling for notifications that have
no owning call.

Delivery discipline mirrors sms_outbox: AT-LEAST-ONCE, status-guarded claim
(pending->sent/failed), and a per-row commit so a mid-batch failure cannot re-send the
whole batch. Idempotency at the *enqueue* layer (the unique ``dedupe_key``) ensures a
duplicate completion path never creates a second row. Gated by NOTIFICATION_OUTBOX_ENABLED
in the lifespan; the per-cycle latency (poll interval, <=300s) is the SC-004 budget.
"""

import asyncio
import contextlib

from loguru import logger

from usan_api import telnyx_messaging
from usan_api.db.session import get_session_factory
from usan_api.observability.custom_metrics import SMS_MESSAGES_TOTAL
from usan_api.repositories import sms_messages as sms_repo
from usan_api.settings import Settings, get_settings

# Per-cycle batch ceiling. Notifications are low-volume (per crisis/missed call), so a
# modest cap keeps each cycle bounded without starving a backlog over several cycles.
_BATCH = 50


async def flush_pending_notifications() -> None:
    """One outbox cycle. Never raises (runs in a fire-and-forget poller)."""
    try:
        await _flush_pending_notifications()
    except Exception as exc:  # noqa: BLE001 - a poller cycle must never crash the loop
        # Log only the exception TYPE, never str(exc): a DB/httpx message can embed SQL
        # params or URLs tied to elder/family phone numbers (PHI-adjacent).
        logger.bind(err=type(exc).__name__).error("Notification outbox flush crashed")


async def _flush_pending_notifications() -> None:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as db:
        pending = await sms_repo.get_pending_notifications(db, limit=_BATCH)
        if not pending:
            return

        if not settings.telnyx_messaging_enabled:
            # Do NOT burn crisis/missed-call alerts when messaging is misconfigured-off:
            # leave them pending so the backlog flushes once messaging is enabled.
            logger.bind(n=len(pending)).warning(
                "Notification outbox: messaging disabled; leaving {n} pending", n=len(pending)
            )
            return

        flushed = 0
        for row in pending:
            try:
                message_id = await telnyx_messaging.send_sms(
                    settings, to_number=row.to_number, body=row.body
                )
            except Exception as exc:  # noqa: BLE001 - any send failure marks the row failed
                claimed = await sms_repo.mark_failed(
                    db, row.id, error={"reason": "send_failed", "detail": type(exc).__name__}
                )
                await db.commit()
                if claimed is not None:
                    SMS_MESSAGES_TOTAL.labels(status="failed").inc()
                    flushed += 1
                continue
            claimed = await sms_repo.mark_sent(db, row.id, telnyx_message_id=message_id)
            # Commit BEFORE moving on: the Telnyx send already happened, so this row's
            # claim must not ride on later rows' fate (at-least-once bound, like sms_outbox).
            await db.commit()
            if claimed is not None:
                SMS_MESSAGES_TOTAL.labels(status="sent").inc()
                flushed += 1
        if flushed:
            logger.bind(n=flushed).info("Notification outbox flush complete")


async def run_poller(settings: Settings, stop: asyncio.Event) -> None:
    """Loop flush_pending_notifications on the configured interval until ``stop`` is set.

    Survives per-cycle exceptions. The interval sleep is a cancellable wait on ``stop``,
    so shutdown is prompt. The interval is the SC-004 dispatch-latency budget (<=300s).
    """
    log = logger.bind(component="notification_outbox")
    log.info(
        "Notification outbox poller started (interval={i}s)",
        i=settings.notification_outbox_poll_interval_s,
    )
    while not stop.is_set():
        try:
            await flush_pending_notifications()
        except Exception:
            log.opt(exception=True).error("Notification outbox cycle failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                stop.wait(), timeout=settings.notification_outbox_poll_interval_s
            )
    log.info("Notification outbox poller stopped")
