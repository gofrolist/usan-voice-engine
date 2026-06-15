"""T072 (US8): monthly family report generation + PHI-minimized delivery.

The ``family_report_job`` poll cycle generates one per-contact status-and-trends report for
the just-completed calendar month (FR-012), aggregating the month's calls / mood / med
adherence into the report row (PHI, stays in Postgres) and enqueuing a FIXED, PHI-FREE
SMS to the family contact (Constitution II / T083 — no clinical content leaves over SMS).
It is once-per-month idempotent (SC-012), and when no family contact is registered the
report is marked ``no_contact`` so operators can follow up (FR-013).
"""

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api import family_report_job
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, FamilyReport, SmsMessage
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo
from usan_api.repositories import medications as medications_repo
from usan_api.repositories import wellness as wellness_repo
from usan_api.settings import Settings

# "Now" is mid-June 2026; the report period is therefore May 2026 (the prior month).
NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
PERIOD = date(2026, 5, 1)
IN_PERIOD = datetime(2026, 5, 10, 15, 0, tzinfo=UTC)

_CLINICAL_TERMS = ("mood", "pain", "medication", "lonely", "loneliness", "satisfaction")


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    async with session_factory() as db:
        await db.execute(
            text(
                "TRUNCATE family_reports, sms_messages, wellness_logs, medication_logs, "
                "family_contacts, calls, contacts CASCADE"
            )
        )
        await db.commit()


def _settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql://u:p@host/db",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "JWT_SIGNING_KEY": "s" * 32,
        "OPERATOR_API_KEY": "o" * 32,
    }
    base.update(overrides)
    return Settings(**base)


def _phone() -> str:
    return f"+1555{str(uuid.uuid4().int)[:7]}"


async def _seed_contact_with_month_data(
    session_factory, *, with_contact: bool, with_calls: bool = True
) -> uuid.UUID:
    async with session_factory() as db:
        contact = await contacts_repo.create_contact(
            db, name="Ada", phone_e164=_phone(), timezone="UTC"
        )
        if with_contact:
            await db.execute(
                text(
                    "INSERT INTO family_contacts (contact_id, name, phone_e164) "
                    "VALUES (:e, 'Dana', :p)"
                ),
                {"e": contact.id, "p": _phone()},
            )
        if with_calls:
            call = await calls_repo.create_call(
                db,
                contact_id=contact.id,
                direction=CallDirection.OUTBOUND,
                status=CallStatus.COMPLETED,
            )
            await db.execute(update(Call).where(Call.id == call.id).values(answered_at=IN_PERIOD))
            wl = await wellness_repo.create_wellness_log(
                db, call_id=call.id, contact_id=contact.id, mood=4, pain_level=1, notes=None
            )
            ml = await medications_repo.create_medication_log(
                db,
                call_id=call.id,
                contact_id=contact.id,
                medication_name="vitamin",
                taken=True,
                reported_time=None,
            )
            await db.execute(
                text("UPDATE wellness_logs SET logged_at = :t WHERE id = :i"),
                {"t": IN_PERIOD, "i": wl.id},
            )
            await db.execute(
                text("UPDATE medication_logs SET logged_at = :t WHERE id = :i"),
                {"t": IN_PERIOD, "i": ml.id},
            )
        await db.commit()
        return contact.id


async def _reports(session_factory, contact_id: uuid.UUID) -> list[FamilyReport]:
    async with session_factory() as db:
        rows = (
            await db.execute(select(FamilyReport).where(FamilyReport.contact_id == contact_id))
        ).scalars()
        return list(rows)


async def _report_sms(session_factory, contact_id: uuid.UUID) -> list[SmsMessage]:
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(SmsMessage).where(
                    SmsMessage.contact_id == contact_id, SmsMessage.kind == "family_report"
                )
            )
        ).scalars()
        return list(rows)


async def test_monthly_report_generated_and_family_sms_phi_free(session_factory):
    contact_id = await _seed_contact_with_month_data(session_factory, with_contact=True)

    counts = await family_report_job.poll_once(session_factory, _settings(), now=NOW)
    assert counts["generated"] == 1

    reports = await _reports(session_factory, contact_id)
    assert len(reports) == 1
    report = reports[0]
    assert report.period_month == PERIOD
    assert report.calls_completed == 1
    assert report.status == "sent"
    # Rich trends are retained internally (PHI stays in Postgres).
    assert report.metrics.get("avg_mood") is not None
    assert report.metrics.get("med_adherence") is not None
    assert report.narrative  # a narrative was produced (LLM or deterministic fallback)

    # The family SMS is PHI-minimized: a non-call notification with NO clinical content.
    sms = await _report_sms(session_factory, contact_id)
    assert len(sms) == 1
    body = sms[0].body.lower()
    assert sms[0].call_id is None
    assert sms[0].status == "pending"
    for term in _CLINICAL_TERMS:
        assert term not in body


async def test_report_idempotent_once_per_month(session_factory):
    contact_id = await _seed_contact_with_month_data(session_factory, with_contact=True)

    await family_report_job.poll_once(session_factory, _settings(), now=NOW)
    await family_report_job.poll_once(session_factory, _settings(), now=NOW)

    assert len(await _reports(session_factory, contact_id)) == 1
    assert len(await _report_sms(session_factory, contact_id)) == 1


async def test_no_family_contact_routes_to_operator(session_factory):
    contact_id = await _seed_contact_with_month_data(session_factory, with_contact=False)

    counts = await family_report_job.poll_once(session_factory, _settings(), now=NOW)
    assert counts["generated"] == 1

    report = (await _reports(session_factory, contact_id))[0]
    # Absence of a family contact is surfaced to operators, not silently dropped (FR-013).
    assert report.status == "no_contact"
    assert await _report_sms(session_factory, contact_id) == []


async def test_contact_with_no_calls_skipped(session_factory):
    contact_id = await _seed_contact_with_month_data(
        session_factory, with_contact=True, with_calls=False
    )

    counts = await family_report_job.poll_once(session_factory, _settings(), now=NOW)
    assert counts["generated"] == 0
    assert await _reports(session_factory, contact_id) == []


async def test_report_narrative_uses_vertex_when_enabled(session_factory, monkeypatch):
    # M8: with summarization enabled + a project, the report narrative comes from Vertex
    # (model_version == the model), not the deterministic fallback. The family SMS is STILL
    # the fixed PHI-free template — the Vertex narrative stays in Postgres (Constitution II).
    from types import SimpleNamespace

    contact_id = await _seed_contact_with_month_data(session_factory, with_contact=True)

    async def _fake_turn(**kwargs):
        return SimpleNamespace(text="Ada stayed engaged and upbeat this month.")

    monkeypatch.setattr(family_report_job, "run_vertex_turn", _fake_turn)
    settings = _settings(
        SUMMARIZATION_ENABLED="true",
        GCP_PROJECT="proj-x",
        SUMMARIZATION_MODEL="gemini-2.5-flash",
    )

    counts = await family_report_job.poll_once(session_factory, settings, now=NOW)
    assert counts["generated"] == 1

    reports = await _reports(session_factory, contact_id)
    assert len(reports) == 1
    assert reports[0].narrative == "Ada stayed engaged and upbeat this month."
    assert reports[0].model_version == "gemini-2.5-flash"  # the Vertex branch, not deterministic

    sms = await _report_sms(session_factory, contact_id)
    assert len(sms) == 1
    low = sms[0].body.lower()
    for term in _CLINICAL_TERMS:
        assert term not in low, f"family report SMS leaks clinical term: {term}"
