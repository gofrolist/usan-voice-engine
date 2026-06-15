"""T035 (US3): log_medication maintains the medication_reminders re-ask state machine.

Contract (contracts/tools-api.md): ``taken=false`` opens/refreshes a ``pending`` reminder
for that medication; ``taken=true`` clears any pending reminder for it. No request/response
shape change. State machine (data-model §medication_reminders): not-taken → ``pending``
(attempt_count=0); each repeated not-taken increments; confirmation → ``cleared``; the cap
(driven in test_medication_reminders_flow) → ``capped`` + a routine follow_up_flags row.

Written FIRST (Constitution IV) — fails until the repo + endpoint extension land.
"""

import time
import uuid

import jwt
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import livekit_dispatch
from usan_api.repositories.medication_reminders import MAX_REASK_ATTEMPTS

_OP = {"Authorization": "Bearer " + "o" * 32}


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _service_token(call_id: str, secret: str = "s" * 32) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )


def _auth(call_id: str) -> dict:
    return {"Authorization": f"Bearer {_service_token(call_id)}"}


async def _make_elder(session_factory) -> str:
    async with session_factory() as db:
        eid = (
            await db.execute(
                text(
                    "INSERT INTO elders (name, phone_e164, timezone) "
                    "VALUES ('Ada', :p, 'UTC') RETURNING id"
                ),
                {"p": f"+1555{str(uuid.uuid4().int)[:7]}"},
            )
        ).scalar_one()
        await db.commit()
        return str(eid)


async def _reminders(session_factory, elder_id: str):
    async with session_factory() as db:
        return (
            await db.execute(
                text(
                    "SELECT medication_name, status, attempt_count, opened_call_id, "
                    "cleared_call_id FROM medication_reminders WHERE elder_id = :e "
                    "ORDER BY id"
                ),
                {"e": elder_id},
            )
        ).all()


async def _med_flags(session_factory, elder_id: str):
    async with session_factory() as db:
        return (
            await db.execute(
                text(
                    "SELECT severity, category FROM follow_up_flags "
                    "WHERE elder_id = :e AND category = 'medication' ORDER BY id"
                ),
                {"e": elder_id},
            )
        ).all()


def _enqueue_call(client, elder_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": f"med-{uuid.uuid4()}", "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


def _log_med(client, call_id: str, name: str, *, taken: bool):
    return client.post(
        "/v1/tools/log_medication",
        json={"call_id": call_id, "medication_name": name, "taken": taken},
        headers=_auth(call_id),
    )


async def test_not_taken_opens_pending_reminder(client, mock_dispatch, session_factory):
    elder_id = await _make_elder(session_factory)
    call_id = _enqueue_call(client, elder_id)

    r = _log_med(client, call_id, "Lisinopril", taken=False)
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["id"], int)  # response shape unchanged (medication_log id)

    rows = await _reminders(session_factory, elder_id)
    assert len(rows) == 1
    assert rows[0].medication_name == "Lisinopril"
    assert rows[0].status == "pending"
    assert rows[0].attempt_count == 0
    assert str(rows[0].opened_call_id) == call_id
    assert rows[0].cleared_call_id is None


async def test_taken_clears_pending_reminder(client, mock_dispatch, session_factory):
    elder_id = await _make_elder(session_factory)
    call_id = _enqueue_call(client, elder_id)

    assert _log_med(client, call_id, "Lisinopril", taken=False).status_code == 200
    # Confirmation on a later turn / call clears the pending re-ask.
    assert _log_med(client, call_id, "Lisinopril", taken=True).status_code == 200

    rows = await _reminders(session_factory, elder_id)
    assert len(rows) == 1
    assert rows[0].status == "cleared"
    assert str(rows[0].cleared_call_id) == call_id
    # Nothing pending remains for the med.
    assert [r for r in rows if r.status == "pending"] == []


async def test_repeated_not_taken_refreshes_single_pending_row(
    client, mock_dispatch, session_factory
):
    # The partial-unique (one pending per (elder, med)) means a repeated not-taken
    # report does NOT create a duplicate — it refreshes the same row and increments.
    elder_id = await _make_elder(session_factory)
    call_id = _enqueue_call(client, elder_id)

    assert _log_med(client, call_id, "Lisinopril", taken=False).status_code == 200
    assert _log_med(client, call_id, "Lisinopril", taken=False).status_code == 200

    rows = await _reminders(session_factory, elder_id)
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].attempt_count == 1  # one re-ask increment over the initial open


async def test_taken_with_no_pending_is_noop(client, mock_dispatch, session_factory):
    elder_id = await _make_elder(session_factory)
    call_id = _enqueue_call(client, elder_id)

    # Confirming a med that was never flagged not-taken simply logs; no reminder row.
    assert _log_med(client, call_id, "Metformin", taken=True).status_code == 200
    assert await _reminders(session_factory, elder_id) == []


async def test_distinct_meds_get_independent_reminders(client, mock_dispatch, session_factory):
    elder_id = await _make_elder(session_factory)
    call_id = _enqueue_call(client, elder_id)

    assert _log_med(client, call_id, "Lisinopril", taken=False).status_code == 200
    assert _log_med(client, call_id, "Metformin", taken=False).status_code == 200
    # Clearing one leaves the other pending.
    assert _log_med(client, call_id, "Lisinopril", taken=True).status_code == 200

    rows = {r.medication_name: r.status for r in await _reminders(session_factory, elder_id)}
    assert rows == {"Lisinopril": "cleared", "Metformin": "pending"}


async def test_clear_pending_works_across_calls(client, mock_dispatch, session_factory):
    # The real product flow: not-taken on one day's call, confirmed on a LATER call (a
    # different call_id). clear_pending matches by (elder, med, pending) regardless of which
    # call opened it, and stamps the CLEARING call. A regression scoping the clear to the
    # opener would leave the reminder pending and nag forever.
    elder_id = await _make_elder(session_factory)
    call_a = _enqueue_call(client, elder_id)
    assert _log_med(client, call_a, "Lisinopril", taken=False).status_code == 200

    call_b = _enqueue_call(client, elder_id)
    assert _log_med(client, call_b, "Lisinopril", taken=True).status_code == 200

    rows = await _reminders(session_factory, elder_id)
    assert len(rows) == 1
    assert rows[0].status == "cleared"
    assert str(rows[0].opened_call_id) == call_a
    assert str(rows[0].cleared_call_id) == call_b  # the CLEARING call, not the opener


async def test_reopen_after_capped_starts_a_new_cycle(client, mock_dispatch, session_factory):
    # Per-cycle cap (FR-019): after a med caps, a FRESH not-taken report is a new clinical
    # signal that opens a NEW pending cycle (attempt_count 0) — but does NOT raise a second
    # routine flag on the reopen itself (only when that new cycle later re-caps).
    elder_id = await _make_elder(session_factory)
    call_id = _enqueue_call(client, elder_id)
    for _ in range(MAX_REASK_ATTEMPTS + 1):  # 1 open + MAX increments -> capped
        assert _log_med(client, call_id, "Lisinopril", taken=False).status_code == 200
    assert [r.status for r in await _reminders(session_factory, elder_id)] == ["capped"]
    assert len(await _med_flags(session_factory, elder_id)) == 1

    # A new not-taken report after the cap reopens a fresh pending cycle...
    assert _log_med(client, call_id, "Lisinopril", taken=False).status_code == 200
    rows = await _reminders(session_factory, elder_id)
    assert [r.status for r in rows] == ["capped", "pending"]
    assert rows[1].attempt_count == 0
    # ...without immediately double-flagging the operator queue.
    assert len(await _med_flags(session_factory, elder_id)) == 1


async def test_partial_unique_blocks_a_second_pending_row(session_factory):
    # The DB partial-unique index (WHERE status='pending') is the real guard behind the
    # refresh-don't-duplicate behavior: two pending rows for one (elder, med) are impossible
    # even under a concurrent insert. (cleared/capped rows are unconstrained — history.)
    elder_id = await _make_elder(session_factory)
    insert = (
        "INSERT INTO medication_reminders (elder_id, medication_name, status) "
        "VALUES (CAST(:e AS uuid), 'Lisinopril', 'pending')"
    )

    async def _insert_pending() -> None:
        async with session_factory() as db:
            await db.execute(text(insert), {"e": elder_id})
            await db.commit()

    await _insert_pending()
    with pytest.raises(IntegrityError):
        await _insert_pending()
