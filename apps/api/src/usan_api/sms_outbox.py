"""Post-call SMS flush (design §6.3). Runs via FastAPI BackgroundTasks AFTER the
response, so it OPENS ITS OWN session (the request session is already closed).

Idempotent: each row is claimed by a status-guarded pending->sent/failed
transition (sms_messages repo), so both completion paths (end_call + the
room_finished webhook) can fire for one call without re-sending. Gated on
TELNYX_MESSAGING_ENABLED; when off, rows are marked failed with a documented
reason (observable, not silent). The metric is incremented AFTER commit.
"""

import uuid

from loguru import logger

from usan_api import telnyx_messaging
from usan_api.db.session import get_session_factory
from usan_api.observability.custom_metrics import SMS_MESSAGES_TOTAL
from usan_api.repositories import sms_messages as sms_repo
from usan_api.settings import get_settings


async def flush_pending_sms(call_id: uuid.UUID) -> None:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as db:
        pending = await sms_repo.get_pending_for_call(db, call_id)
        if not pending:
            return

        if not settings.telnyx_messaging_enabled:
            for row in pending:
                await sms_repo.mark_failed(db, row.id, error={"reason": "messaging_disabled"})
            await db.commit()
            for _ in pending:
                SMS_MESSAGES_TOTAL.labels(status="failed").inc()
            logger.bind(call_id=str(call_id), n=len(pending)).info(
                "SMS flush skipped: messaging disabled"
            )
            return

        results: list[str] = []
        for row in pending:
            try:
                message_id = await telnyx_messaging.send_sms(
                    settings, to_number=row.to_number, body=row.body
                )
            except Exception as exc:  # noqa: BLE001 - any send failure marks the row failed
                # Store only the exception TYPE, never str(exc): the error column is
                # PHI-adjacent (tied to elder phone numbers) and an httpx error string
                # can leak URL query strings or response-body fragments. PHI-free, like
                # the SMS_MESSAGES_TOTAL counter (custom_metrics.py).
                await sms_repo.mark_failed(
                    db,
                    row.id,
                    error={"reason": "send_failed", "detail": type(exc).__name__},
                )
                results.append("failed")
                continue
            await sms_repo.mark_sent(db, row.id, telnyx_message_id=message_id)
            results.append("sent")
        await db.commit()
        for outcome in results:
            SMS_MESSAGES_TOTAL.labels(status=outcome).inc()
        logger.bind(call_id=str(call_id), n=len(results)).info("SMS flush complete")
