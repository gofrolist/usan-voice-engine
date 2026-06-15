"""T024 (US2): missed-call family alerts + operator fallback at finalization.

When schedule_retry exhausts the retry policy (FR-010), the family contact opted in to
missed-call alerts gets a PHI-minimized SMS (deduped per call); when no family contact
is registered, the miss is surfaced to the operator queue as a routine follow_up_flag
(FR-013). A contact who opted out of missed-call alerts triggers neither.
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import FollowUpFlag, SmsMessage
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo

FIXED_NOW = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
_FAMILY = "+15557654321"


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(calls_repo, "_utcnow", lambda: FIXED_NOW)


@pytest.fixture(autouse=True)
async def _truncate(session_factory):
    async with session_factory() as db:
        await db.execute(
            text(
                "TRUNCATE family_tasks, family_contacts, follow_up_flags, sms_messages, "
                "calls, contacts RESTART IDENTITY CASCADE"
            )
        )
        await db.commit()


async def _seed_exhausted(session_factory, *, contact: bool = True, prefs: dict | None = None):
    """A NO_ANSWER call at attempt=3 (default policy stops at 2) → schedule_retry None."""
    async with session_factory() as db:
        contact_row = await contacts_repo.create_contact(
            db, name="A", phone_e164=f"+1555{str(uuid.uuid4().int)[:7]}", timezone="UTC"
        )
        call = await calls_repo.create_call(
            db,
            contact_id=contact_row.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.NO_ANSWER,
            dynamic_vars={},
            livekit_room="usan-outbound-parent",
        )
        call.attempt = 3
        await db.flush()
        if contact:
            await db.execute(
                text(
                    "INSERT INTO family_contacts (contact_id, name, phone_e164, alert_prefs) "
                    "VALUES (:e, 'Dana', :p, CAST(:pr AS JSONB))"
                ),
                {"e": contact_row.id, "p": _FAMILY, "pr": json.dumps(prefs or {})},
            )
        await db.commit()
        return call.id


async def _missed_alerts(session_factory, call_id) -> list[SmsMessage]:
    async with session_factory() as db:
        rows = (
            (await db.execute(select(SmsMessage).where(SmsMessage.call_id.is_(None))))
            .scalars()
            .all()
        )
        return [r for r in rows if r.dedupe_key and r.dedupe_key.startswith(f"missed:{call_id}:")]


async def _operator_flags(session_factory, call_id) -> list[FollowUpFlag]:
    async with session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(FollowUpFlag).where(
                        FollowUpFlag.call_id == call_id,
                        FollowUpFlag.category == "operator_alert",
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


async def _run_schedule_retry(session_factory, call_id):
    async with session_factory() as db:
        result = await calls_repo.schedule_retry(db, call_id)
        await db.commit()
        return result


async def test_missed_call_alerts_opted_in_contact(session_factory):
    call_id = await _seed_exhausted(session_factory, contact=True)
    assert await _run_schedule_retry(session_factory, call_id) is None  # retries exhausted
    alerts = await _missed_alerts(session_factory, call_id)
    assert len(alerts) == 1
    assert alerts[0].kind == "family_alert"
    assert alerts[0].to_number == _FAMILY
    low = alerts[0].body.lower()
    for term in ("mood", "pain", "medication"):
        assert term not in low  # PHI-minimized
    assert await _operator_flags(session_factory, call_id) == []  # family present → no op flag


async def test_missed_call_no_contact_creates_operator_flag(session_factory):
    call_id = await _seed_exhausted(session_factory, contact=False)
    assert await _run_schedule_retry(session_factory, call_id) is None
    assert await _missed_alerts(session_factory, call_id) == []
    flags = await _operator_flags(session_factory, call_id)
    assert len(flags) == 1  # FR-013: surfaced to the operator queue
    assert flags[0].severity == "routine"
    assert "family" in (flags[0].reason or "").lower()


async def test_missed_call_opt_out_no_alert_no_operator_flag(session_factory):
    # A registered contact who opted out of missed-call alerts: no SMS, and NOT an
    # operator fallback (a contact IS registered — they chose not to be alerted).
    call_id = await _seed_exhausted(session_factory, contact=True, prefs={"missed_call": False})
    assert await _run_schedule_retry(session_factory, call_id) is None
    assert await _missed_alerts(session_factory, call_id) == []
    assert await _operator_flags(session_factory, call_id) == []


async def test_missed_call_alert_idempotent_across_reentry(session_factory):
    call_id = await _seed_exhausted(session_factory, contact=True)
    await _run_schedule_retry(session_factory, call_id)
    await _run_schedule_retry(session_factory, call_id)  # re-entry (e.g. recovery)
    assert len(await _missed_alerts(session_factory, call_id)) == 1  # deduped per call


async def test_missed_operator_flag_idempotent_across_reentry(session_factory):
    call_id = await _seed_exhausted(session_factory, contact=False)
    await _run_schedule_retry(session_factory, call_id)
    await _run_schedule_retry(session_factory, call_id)
    assert len(await _operator_flags(session_factory, call_id)) == 1
