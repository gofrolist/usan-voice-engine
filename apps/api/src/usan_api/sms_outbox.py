"""Post-call SMS flush (design §6.3). Runs via FastAPI BackgroundTasks AFTER the
response, so it OPENS ITS OWN session (the request session is already closed).

Idempotent: each row is claimed by a status-guarded pending->sent/failed
transition (sms_messages repo), so both completion paths (end_call + the
room_finished webhook) can fire for one call without re-sending. Gated on
TELNYX_MESSAGING_ENABLED; when off, rows are marked failed with a documented
reason (observable, not silent). The metric is incremented AFTER each row's commit.

Delivery is AT-LEAST-ONCE: the Telnyx POST and the status commit cannot be made
one atomic step, so a commit that fails after a successful send leaves that row
pending and a later flush re-sends it. Each row is committed INDIVIDUALLY, right
after its claim, which bounds the duplicate window to the single row whose commit
failed — a loop-end commit would instead re-send the whole batch.
"""

import uuid

from loguru import logger

from usan_api import telnyx_messaging
from usan_api.db.session import get_session_factory
from usan_api.observability.custom_metrics import SMS_MESSAGES_TOTAL
from usan_api.repositories import sms_messages as sms_repo
from usan_api.settings import get_settings


async def flush_pending_sms(call_id: uuid.UUID) -> None:
    try:
        await _flush_pending_sms(call_id)
    except Exception as exc:  # noqa: BLE001 - fire-and-forget background task
        # FastAPI's BackgroundTasks runner swallows exceptions silently — without
        # this the rows would sit pending with no log at all. Log only the exception
        # TYPE, never str(exc): a DB/httpx message can embed SQL parameters or URLs
        # (PHI-adjacent, same rule as the error column below).
        logger.bind(call_id=str(call_id), err=type(exc).__name__).error("SMS flush crashed")


async def _flush_pending_sms(call_id: uuid.UUID) -> None:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as db:
        pending = await sms_repo.get_pending_for_call(db, call_id)
        if not pending:
            return

        if not settings.telnyx_messaging_enabled:
            # No external side effects on this path, so one batch commit is safe.
            disabled = 0
            for row in pending:
                claimed = await sms_repo.mark_failed(
                    db, row.id, error={"reason": "messaging_disabled"}
                )
                # Skip the count when a concurrent flush already claimed the row, so the
                # SMS_MESSAGES_TOTAL counter can't double-increment in a completion race.
                if claimed is not None:
                    disabled += 1
            await db.commit()
            for _ in range(disabled):
                SMS_MESSAGES_TOTAL.labels(status="failed").inc()
            logger.bind(call_id=str(call_id), n=disabled).info(
                "SMS flush skipped: messaging disabled"
            )
            return

        flushed = 0
        for row in pending:
            try:
                message_id = await telnyx_messaging.send_sms(
                    settings, to_number=row.to_number, body=row.body
                )
            except Exception as exc:  # noqa: BLE001 - any send failure marks the row failed
                # Store only the exception TYPE, never str(exc): the error column is
                # PHI-adjacent (tied to contact phone numbers) and an httpx error string
                # can leak URL query strings or response-body fragments. PHI-free, like
                # the SMS_MESSAGES_TOTAL counter (custom_metrics.py).
                claimed = await sms_repo.mark_failed(
                    db,
                    row.id,
                    error={"reason": "send_failed", "detail": type(exc).__name__},
                )
                await db.commit()
                # Only count when THIS flush claimed the row; a None means a concurrent
                # flush (end_call + room_finished race) already transitioned it, so
                # counting here would double-increment SMS_MESSAGES_TOTAL.
                if claimed is not None:
                    SMS_MESSAGES_TOTAL.labels(status="failed").inc()
                    flushed += 1
                continue
            claimed = await sms_repo.mark_sent(db, row.id, telnyx_message_id=message_id)
            # Commit BEFORE moving on: the Telnyx send above already happened, so this
            # row's claim must not ride on later rows' fate (at-least-once bound).
            await db.commit()
            if claimed is not None:
                SMS_MESSAGES_TOTAL.labels(status="sent").inc()
                flushed += 1
        logger.bind(call_id=str(call_id), n=flushed).info("SMS flush complete")
