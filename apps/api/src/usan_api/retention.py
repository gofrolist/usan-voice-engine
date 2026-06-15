"""PHI retention purge (§10 data retention).

When PHI_RETENTION_DAYS is set, ``run_poller`` loops the purge once per day. It:
- deletes transcript rows past the cutoff and strips ``dynamic_vars`` (which may embed
  elder names / last-check-in summaries) + ``recording_uri`` from terminal calls and
  settled batch targets past the cutoff (``purge_expired``); and
- deletes the standalone elder-memory PHI records past the cutoff — per-call recaps,
  monthly survey scores, monthly family reports, and extracted/stated facts
  (``purge_memory_phi``; Clara Care Parity tables, review M5).
Default-off: with PHI_RETENTION_DAYS unset the poller never starts, so existing
deployments retain everything. ``call_schedules.dynamic_vars`` stays exempt as live
re-used config (operator PATCH/DELETE is its removal path).
"""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from loguru import logger
from sqlalchemy import CursorResult, delete, func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usan_api.db.base import CallStatus
from usan_api.db.models import (
    Call,
    CallBatchTarget,
    ConversationSummary,
    FamilyReport,
    PersonalFact,
    Transcript,
    WellbeingSurveyResult,
)
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
) -> tuple[int, int, int]:
    """Delete old transcripts and scrub PHI off old terminal calls + settled batch targets.

    Returns ``(transcripts_deleted, calls_scrubbed, batch_targets_scrubbed)``. The
    caller commits — this keeps all three mutations in a single transaction so a
    crash can't leave a partial purge. Nulling recording_uri stops the API issuing
    fresh signed URLs for audio whose retention window has elapsed. ``now``
    overrides the clock for tests.
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

    # Scrub the *source* copy too: batch-target dynamic_vars are copied onto the
    # call at materialization, so without this the calls copy is scrubbed while
    # the source copy lives forever, defeating the control (spec §8). Settled
    # targets only; call_schedules.dynamic_vars are deliberately exempt as live
    # re-used config — the operator PHI-removal path there is PATCH/DELETE.
    target_scrub = cast(
        "CursorResult[Any]",
        await session.execute(
            update(CallBatchTarget)
            .where(
                CallBatchTarget.status.in_(("done", "skipped", "cancelled")),
                func.coalesce(CallBatchTarget.finalized_at, CallBatchTarget.updated_at) < cutoff,
                CallBatchTarget.dynamic_vars != {},
            )
            .values(dynamic_vars={})
        ),
    )
    return deleted.rowcount or 0, scrubbed.rowcount or 0, target_scrub.rowcount or 0


# Elder-memory PHI added by Clara Care Parity (002). Unlike call dynamic_vars (scrubbed in
# place), these are standalone PHI records keyed by their own created_at, so once older than
# the window they are DELETED outright. A fact still actively mentioned is re-extracted with a
# fresh timestamp on the next call, so live memory survives while abandoned PHI is purged.
_MEMORY_PHI_MODELS = (
    ("conversation_summaries", ConversationSummary),
    ("wellbeing_survey_results", WellbeingSurveyResult),
    ("family_reports", FamilyReport),
    ("personal_facts", PersonalFact),
)


async def purge_memory_phi(
    session: AsyncSession, *, days: int, now: datetime | None = None
) -> dict[str, int]:
    """Delete elder-memory PHI rows older than the retention window (review M5).

    Returns a ``{table: rows_deleted}`` map. The caller commits — these run in the SAME
    transaction as ``purge_expired`` so a crash can't leave a partial purge. ``now``
    overrides the clock for tests.
    """
    cutoff = (now if now is not None else _utcnow()) - timedelta(days=days)
    counts: dict[str, int] = {}
    for label, model in _MEMORY_PHI_MODELS:
        result = cast(
            "CursorResult[Any]",
            await session.execute(delete(model).where(model.created_at < cutoff)),
        )
        counts[label] = result.rowcount or 0
    return counts


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
        transcripts, calls, batch_targets = await purge_expired(db, days=days)
        memory = await purge_memory_phi(db, days=days)
        await db.commit()
    memory_total = sum(memory.values())
    if transcripts or calls or batch_targets or memory_total:
        logger.bind(component="retention_poller").info(
            "Purged {t} transcript(s) + {m} memory-PHI row(s); scrubbed dynamic_vars on "
            "{c} call(s) and {b} batch target(s)",
            t=transcripts,
            m=memory_total,
            c=calls,
            b=batch_targets,
        )
