"""Monthly family report job (US8 / T078; FR-012 / SC-012).

Once a month, for each contact with at least one call in the just-completed calendar month,
generate a status-and-trends report and deliver it to the family contact. The aggregated
trends (calls, mood, med adherence, survey) and the LLM narrative are PHI and stay on the
``family_reports`` row in BAA Postgres; the family SMS is a FIXED, PHI-FREE template
(Constitution II / T083) that only signals the contact is engaged. When no family contact is
registered the report is marked ``no_contact`` so operators follow up (FR-013).

Idempotent: one report per ``(contact, period_month)``. The narrative uses Vertex AI
(``vertexai=True`` + ADC, NEVER the Gemini Developer API) when summarization is configured,
else a deterministic non-LLM fallback. Ship-inert: wired only when
``family_report_poller_enabled`` is set (main.py lifespan).
"""

import asyncio
import contextlib
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usan_api import notifications
from usan_api.db.base import CallStatus
from usan_api.db.models import Call, Contact, MedicationLog, WellnessLog
from usan_api.db.session import get_session_factory
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import family_contacts as family_contacts_repo
from usan_api.repositories import family_reports as family_reports_repo
from usan_api.repositories import survey_results as survey_results_repo
from usan_api.settings import Settings
from usan_api.vertex_test import run_vertex_turn

_MAX_NARRATIVE_CHARS = 4000

# Per-cycle ceiling on NEW reports generated, so one cycle cannot fire an unbounded burst
# of multi-second Vertex calls across a large contact population (review: resource exhaustion).
# Already-reported contacts are skipped cheaply (get_for_month) and don't count, so the cycle
# advances ~_GENERATE_BUDGET fresh reports at a time; the rest follow on later cycles.
_GENERATE_BUDGET = 100
# Hard bound on a single narrative round-trip so a hung Vertex call can't stall the poller
# (run_vertex_turn has no internal timeout). On timeout the report uses the deterministic body.
_VERTEX_TIMEOUT_S = 30.0

_REPORT_SYSTEM = (
    "You write a brief, warm monthly status note about a person's wellness "
    "check-ins for their care team's internal record. Respond with 1-3 plain sentences, "
    "factual and kind. Do not invent details beyond the figures provided."
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _previous_month_anchor(now: datetime, timezone: str) -> date:
    """First-of-month DATE for the month BEFORE ``now`` in the contact's local month.

    Reuses the survey anchor (contact-local, fail-soft on a bad tz) so the report period
    aligns with the wellbeing survey month.
    """
    this_month = survey_results_repo.month_anchor(timezone, now)
    return (this_month - timedelta(days=1)).replace(day=1)


def _month_window(period_month: date, timezone: str) -> tuple[datetime, datetime]:
    """[start, end) UTC instants bounding ``period_month`` in the contact's local month."""
    try:
        tz = ZoneInfo(timezone) if timezone else ZoneInfo("UTC")
    except ZoneInfoNotFoundError, ValueError, KeyError:
        tz = ZoneInfo("UTC")
    start_local = datetime(period_month.year, period_month.month, 1, tzinfo=tz)
    if period_month.month == 12:
        next_first = date(period_month.year + 1, 1, 1)
    else:
        next_first = date(period_month.year, period_month.month + 1, 1)
    end_local = datetime(next_first.year, next_first.month, 1, tzinfo=tz)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


async def _aggregate(
    db: AsyncSession,
    *,
    contact_id: Any,
    start: datetime,
    end: datetime,
    period_month: date,
) -> tuple[int, dict[str, Any]]:
    """Aggregate the month's trends. Returns ``(calls_completed, metrics)`` (metrics PHI)."""
    calls_completed = (
        await db.execute(
            select(func.count())
            .select_from(Call)
            .where(
                Call.contact_id == contact_id,
                Call.status == CallStatus.COMPLETED,
                Call.answered_at >= start,
                Call.answered_at < end,
            )
        )
    ).scalar_one()

    avg_mood_raw = (
        await db.execute(
            select(func.avg(WellnessLog.mood)).where(
                WellnessLog.contact_id == contact_id,
                WellnessLog.logged_at >= start,
                WellnessLog.logged_at < end,
            )
        )
    ).scalar_one()
    avg_mood = round(float(avg_mood_raw), 1) if avg_mood_raw is not None else None

    total_meds, taken_meds = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(MedicationLog.taken.is_(True)),
            ).where(
                MedicationLog.contact_id == contact_id,
                MedicationLog.logged_at >= start,
                MedicationLog.logged_at < end,
            )
        )
    ).one()
    med_adherence = round(taken_meds / total_meds, 2) if total_meds else None

    survey = await survey_results_repo.get_for_month(
        db, contact_id=contact_id, period_month=period_month
    )
    survey_summary = (
        {
            "loneliness": survey.loneliness,
            "mood": survey.mood,
            "satisfaction": survey.satisfaction,
        }
        if survey is not None
        else None
    )

    metrics: dict[str, Any] = {
        "calls_completed": calls_completed,
        "avg_mood": avg_mood,
        "med_adherence": med_adherence,
        "survey": survey_summary,
    }
    return calls_completed, metrics


def _fallback_narrative(metrics: dict[str, Any], calls_completed: int) -> str:
    """A deterministic internal narrative when the LLM is unavailable (still PHI, in DB)."""
    parts = [f"{calls_completed} wellness call(s) completed this month."]
    if metrics.get("avg_mood") is not None:
        parts.append(f"Average reported mood {metrics['avg_mood']}/10.")
    if metrics.get("med_adherence") is not None:
        parts.append(f"Medication adherence {round(metrics['med_adherence'] * 100)}%.")
    if metrics.get("survey") is not None:
        parts.append("A monthly wellbeing survey is on file.")
    return " ".join(parts)


async def _narrative(
    metrics: dict[str, Any], calls_completed: int, settings: Settings
) -> tuple[str, str]:
    """Produce the report narrative (LLM when configured, else deterministic)."""
    if settings.summarization_enabled and settings.gcp_project:
        prompt = _fallback_narrative(metrics, calls_completed)
        try:
            turn = await asyncio.wait_for(
                run_vertex_turn(
                    model=settings.summarization_model,
                    temperature=0.3,
                    system_instruction=_REPORT_SYSTEM,
                    tools=[],
                    contents=[{"role": "user", "parts": [{"text": prompt}]}],
                    settings=settings,
                ),
                timeout=_VERTEX_TIMEOUT_S,
            )
            text = (turn.text or "").strip()[:_MAX_NARRATIVE_CHARS]
            if text:
                return text, settings.summarization_model
        except Exception as exc:  # noqa: BLE001 - never fail the report on a model hiccup
            logger.bind(err=type(exc).__name__).warning(
                "family report narrative LLM failed; using fallback"
            )
    return _fallback_narrative(metrics, calls_completed), "deterministic"


async def _generate_one(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    contact_id: Any,
    moment: datetime,
) -> bool:
    """Generate + deliver one contact's report for the prior month. True if a report was made."""
    # Phase 1 — read-only aggregation in a short-lived session. We must NOT hold a DB
    # connection/transaction open across the multi-second Vertex narrative call (review M7),
    # so the session closes before the network round-trip below.
    async with factory() as db:
        contact = await contacts_repo.get_contact(db, contact_id)
        if contact is None:
            return False
        period_month = _previous_month_anchor(moment, contact.timezone)
        if await family_reports_repo.get_for_month(
            db, contact_id=contact_id, period_month=period_month
        ):
            return False  # idempotent: already generated this contact's month
        start, end = _month_window(period_month, contact.timezone)
        calls_completed, metrics = await _aggregate(
            db, contact_id=contact_id, start=start, end=end, period_month=period_month
        )
    if calls_completed == 0:
        return False  # SC-012: only contacts with at least one call in the period

    # Vertex narrative OUTSIDE any DB transaction (no connection held during the network call).
    narrative, model_version = await _narrative(metrics, calls_completed, settings)

    # Phase 2 — persist + enqueue in a fresh session. The get_for_month gap above is safe:
    # create() is ON CONFLICT DO NOTHING, so a worker that lost the race gets report=None.
    async with factory() as db:
        recipients = await family_contacts_repo.list_alert_recipients(
            db, contact_id=contact_id, kind="report"
        )
        has_contact = bool(recipients) or bool(
            await family_contacts_repo.list_family_contacts(db, contact_id=contact_id)
        )
        status = "sent" if has_contact else "no_contact"

        report = await family_reports_repo.create(
            db,
            contact_id=contact_id,
            period_month=period_month,
            calls_completed=calls_completed,
            metrics=metrics,
            narrative=narrative,
            model_version=model_version,
            status=status,
        )
        if report is None:
            return False  # lost the race to a concurrent worker

        if recipients:
            body = notifications.build_family_report_body()
            first_sms = None
            for recipient in recipients:
                sms = await notifications.enqueue_family_report(
                    db,
                    contact_id=contact_id,
                    to_number=recipient.phone_e164,
                    body=body,
                    dedupe_key=(
                        f"family_report:{contact_id}:{period_month.isoformat()}:"
                        f"{recipient.phone_e164}"
                    ),
                )
                first_sms = first_sms or sms
            if first_sms is not None:
                report.sms_message_id = first_sms.id

        await db.commit()
        logger.bind(contact_id=str(contact_id), status=status, calls=calls_completed).info(
            "Generated monthly family report"
        )
        return True


async def poll_once(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """One report cycle: generate the prior month's report for each eligible contact.

    ``now`` overrides the clock for deterministic tests. Each contact is processed in its own
    transaction; the unique ``(contact, period_month)`` makes the whole cycle idempotent.
    """
    moment = now if now is not None else _utcnow()
    async with factory() as db:
        contact_ids = list((await db.execute(select(Contact.id))).scalars())

    generated = 0
    for contact_id in contact_ids:
        if generated >= _GENERATE_BUDGET:
            break  # cap expensive Vertex bursts per cycle; remaining contacts run next cycle
        if await _generate_one(factory, settings, contact_id, moment):
            generated += 1
    return {"generated": generated}


async def run_poller(settings: Settings, stop: asyncio.Event) -> None:
    """Background loop: one cycle every ``family_report_poll_interval_s`` until stopped."""
    logger.bind(interval_s=settings.family_report_poll_interval_s).info(
        "Family report poller started"
    )
    factory = get_session_factory()
    while not stop.is_set():
        try:
            await poll_once(factory, settings)
        except Exception as exc:  # noqa: BLE001 - poller must survive; log TYPE only (PHI-safe)
            logger.bind(err=type(exc).__name__).error("family report cycle failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.family_report_poll_interval_s)
