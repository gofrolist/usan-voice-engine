"""T036 (US3): the medication re-reminder loop, end to end.

Drives the loop the agent + builtins implement: a not-taken med opens a pending re-ask,
which is carried into the NEXT call as the ``pending_med_reasks`` builtin (so the agent
re-asks), and after the cap of repeated not-taken reports the reminder is ``capped`` and a
routine ``follow_up_flags`` row is opened — at which point the med is no longer surfaced,
so Clara stops nagging.

Re-ask delivery is asserted by inspecting the ``resolved_vars`` the call-create path passes
to ``dispatch_agent`` (the out-of-band metadata that carries builtins into the call).

Written FIRST (Constitution IV) — fails until repo + endpoint + builtin wiring land.
"""

import time
import uuid
from unittest.mock import AsyncMock

import jwt
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import OPERATOR_HEADERS as _OP
from tests.conftest import service_token as _service_token
from usan_api import livekit_dispatch
from usan_api.repositories.medication_reminders import MAX_REASK_ATTEMPTS


@pytest.fixture
def dispatch_spy(monkeypatch):
    """Patch dispatch_agent with a spy so we can read the resolved_vars carried per call."""
    from usan_api import dialer

    spy = AsyncMock()
    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", spy)
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)
    return spy


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _auth(call_id: str) -> dict:
    return {"Authorization": f"Bearer {_service_token(call_id)}"}


def _worker_auth(secret: str = "s" * 32) -> dict:
    now = int(time.time())
    token = jwt.encode(
        {"sub": "usan-agent", "iat": now, "exp": now + 300}, secret, algorithm="HS256"
    )
    return {"Authorization": f"Bearer {token}"}


async def _make_contact_with_phone(session_factory) -> tuple[str, str]:
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with session_factory() as db:
        eid = (
            await db.execute(
                text(
                    "INSERT INTO contacts (name, phone_e164, timezone) "
                    "VALUES ('Ada', :p, 'UTC') RETURNING id"
                ),
                {"p": phone},
            )
        ).scalar_one()
        await db.commit()
        return str(eid), phone


async def _make_contact(session_factory) -> str:
    eid, _phone = await _make_contact_with_phone(session_factory)
    return eid


def _enqueue_call(client, contact_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={
            "contact_id": contact_id,
            "idempotency_key": f"med-{uuid.uuid4()}",
            "dynamic_vars": {},
        },
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


def _last_reask(dispatch_spy) -> str:
    """The pending_med_reasks builtin carried into the most recently dispatched call."""
    return dispatch_spy.call_args.kwargs["resolved_vars"]["pending_med_reasks"]


async def _med_flags(session_factory, contact_id: str):
    async with session_factory() as db:
        return (
            await db.execute(
                text(
                    "SELECT severity, category FROM follow_up_flags "
                    "WHERE contact_id = :e AND category = 'medication' ORDER BY id"
                ),
                {"e": contact_id},
            )
        ).all()


async def _reminder_statuses(session_factory, contact_id: str) -> list[str]:
    async with session_factory() as db:
        rows = await db.execute(
            text("SELECT status FROM medication_reminders WHERE contact_id = :e ORDER BY id"),
            {"e": contact_id},
        )
        return [r.status for r in rows.all()]


async def test_reask_carried_next_touch_then_capped_no_nag(client, dispatch_spy, session_factory):
    contact_id = await _make_contact(session_factory)

    # 1. Contact reports a med NOT taken -> opens a pending re-ask.
    call_a = _enqueue_call(client, contact_id)
    assert _log_med(client, call_a, "Lisinopril", taken=False).status_code == 200

    # 2. Re-ask delivered next touch: the following call carries pending_med_reasks.
    _enqueue_call(client, contact_id)
    assert "Lisinopril" in _last_reask(dispatch_spy)

    # 3. Repeated not-taken reports drive the reminder to its cap.
    for _ in range(MAX_REASK_ATTEMPTS):
        assert _log_med(client, call_a, "Lisinopril", taken=False).status_code == 200

    assert await _reminder_statuses(session_factory, contact_id) == ["capped"]
    # Cap hands off to an operator via a routine follow-up flag (not an urgent crisis).
    flags = await _med_flags(session_factory, contact_id)
    assert [(f.severity, f.category) for f in flags] == [("routine", "medication")]

    # 4. No nag: a capped med is no longer surfaced as a re-ask on later calls.
    _enqueue_call(client, contact_id)
    assert "Lisinopril" not in _last_reask(dispatch_spy)


async def test_confirmation_before_cap_clears_and_carries_no_reask(
    client, dispatch_spy, session_factory
):
    contact_id = await _make_contact(session_factory)
    call_a = _enqueue_call(client, contact_id)
    assert _log_med(client, call_a, "Metformin", taken=False).status_code == 200
    # Contact later confirms they took it -> reminder cleared, no routine flag raised.
    assert _log_med(client, call_a, "Metformin", taken=True).status_code == 200

    assert await _reminder_statuses(session_factory, contact_id) == ["cleared"]
    assert await _med_flags(session_factory, contact_id) == []

    _enqueue_call(client, contact_id)
    assert _last_reask(dispatch_spy) == ""  # nothing left to re-ask


async def test_inbound_call_carries_pending_med_reasks(client, dispatch_spy, session_factory):
    # The inbound resolver in routers/calls.py is a SEPARATE hand-written carry site from the
    # outbound path. An contact who reported a med not-taken, then calls IN, must still be
    # re-asked: pending_med_reasks must appear in the inbound response's resolved_vars.
    contact_id, phone = await _make_contact_with_phone(session_factory)
    call_a = _enqueue_call(client, contact_id)
    assert _log_med(client, call_a, "Lisinopril", taken=False).status_code == 200

    resp = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": phone, "livekit_room": "usan-inbound-med"},
        headers=_worker_auth(),
    )
    assert resp.status_code == 200, resp.text
    assert "Lisinopril" in resp.json()["resolved_vars"]["pending_med_reasks"]
