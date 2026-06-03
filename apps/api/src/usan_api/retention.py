"""PHI retention purge (§10 data retention).

When PHI_RETENTION_DAYS is set, ``run_poller`` loops ``purge_expired`` once per
day. The purge deletes transcript rows past the cutoff and strips ``dynamic_vars``
(which may embed elder names / last-check-in summaries) from terminal calls past
the cutoff. Default-off: with PHI_RETENTION_DAYS unset the poller never starts, so
existing deployments retain everything.
"""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from loguru import logger
from sqlalchemy import CursorResult, delete, or_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usan_api.db.base import CallStatus
from usan_api.db.models import Call, Transcript
from usan_api.db.session import get_session_factory
from usan_api.settings import Settings

# A day in seconds; the poller wakes once per day to apply the retention window.
_POLL_INTERVAL_S = 86_400.0

# Calls in these states are finished; their dynamic_vars are safe to strip once the
# retention window has elapsed. Non-terminal calls are left untouched.
_TERMINAL_STATUSES: frozenset[CallStatus] = frozenset(
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


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def purge_expired(
    session: AsyncSession, *, days: int, now: datetime | None = None
) -> tuple[int, int]:
    """Delete old transcripts and null dynamic_vars + recording_uri on old terminal calls.

    Returns ``(transcripts_deleted, calls_scrubbed)``. The caller commits — this
    keeps both mutations in a single transaction so a crash can't leave a partial
    purge. Nulling recording_uri stops the API issuing fresh signed URLs for audio
    whose retention window has elapsed. ``now`` overrides the clock for tests.
    """
    moment = now if now is not None else _utcnow()
    cutoff = moment - timedelta(days=days)

    deleted = cast(
        "CursorResult[Any]",
        await session.execute(delete(Transcript).where(Transcript.created_at < cutoff)),
    )

    scrubbed = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Call)
            .where(
                Call.status.in_(_TERMINAL_STATUSES),
                or_(Call.ended_at < cutoff, Call.ended_at.is_(None) & (Call.created_at < cutoff)),
                # Match a row that still holds PHI in either field, so the rowcount
                # reflects real work and the UPDATE is idempotent on re-run.
                or_(Call.dynamic_vars != {}, Call.recording_uri.is_not(None)),
            )
            # Null recording_uri too: otherwise GET /calls/{id} keeps minting signed
            # URLs for the PHI audio until the (independent) GCS lifecycle rule fires.
            .values(dynamic_vars={}, recording_uri=None)
        ),
    )
    return deleted.rowcount or 0, scrubbed.rowcount or 0


async def run_poller(settings: Settings, stop: asyncio.Event) -> None:
    """Run ``purge_expired`` daily until ``stop`` is set. No-op if retention is unset.

    Mirrors retry_orchestrator.run_poller: survives per-cycle exceptions (logged,
    never fatal) and the interval sleep is a cancellable wait on ``stop``.
    """
    days = settings.phi_retention_days
    if days is None:
        return
    log = logger.bind(component="retention_poller")
    log.info("PHI retention poller started (window={d}d)", d=days)
    factory = get_session_factory()
    while not stop.is_set():
        try:
            await _purge_cycle(factory, days)
        except Exception:
            log.opt(exception=True).error("Retention purge cycle failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_POLL_INTERVAL_S)
    log.info("PHI retention poller stopped")


async def _purge_cycle(factory: async_sessionmaker[AsyncSession], days: int) -> None:
    async with factory() as db:
        transcripts, calls = await purge_expired(db, days=days)
        await db.commit()
    if transcripts or calls:
        logger.bind(component="retention_poller").info(
            "Purged {t} transcript(s); scrubbed dynamic_vars on {c} call(s)",
            t=transcripts,
            c=calls,
        )
