"""T058 (US6): integration tests for the wellbeing flow.

Two threads of US6 wired end-to-end:

1. ``survey_due`` builtin — an contact with no ``wellbeing_survey_results`` this month is
   dispatched with ``survey_due == "true"``; once ``record_survey`` lands the row, the
   next dispatch resolves it to ``""`` (FR-032). This exercises the real
   ``routers/calls`` -> ``survey_results`` repo -> ``resolve_builtin_vars`` -> dispatch path
   by capturing the ``resolved_vars`` handed to the (mocked) LiveKit dispatch.

2. Non-repeating activity sequence — driving ``get_activity`` across many calls yields a
   full non-repeating cycle (FR-034 / SC-009), proving the per-contact ``activity_history``
   selection holds across separate calls/sessions.

Written FIRST (Constitution IV) — fails until the builtin + repos + endpoints + wiring land.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import OPERATOR_HEADERS as _OP
from tests.conftest import service_token as _service_token
from usan_api import livekit_dispatch


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer

    dispatch = AsyncMock()
    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", dispatch)
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)
    return dispatch


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _auth(call_id: str) -> dict:
    return {"Authorization": f"Bearer {_service_token(call_id)}"}


async def _make_contact(session_factory) -> str:
    async with session_factory() as db:
        eid = (
            await db.execute(
                text(
                    "INSERT INTO contacts (name, phone_e164, timezone) "
                    "VALUES ('Bea', :p, 'America/New_York') RETURNING id"
                ),
                {"p": f"+1555{str(uuid.uuid4().int)[:7]}"},
            )
        ).scalar_one()
        await db.commit()
        return str(eid)


def _enqueue_call(client, contact_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={
            "contact_id": contact_id,
            "idempotency_key": f"wf-{uuid.uuid4()}",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


def _last_resolved_vars(mock_dispatch) -> dict:
    assert mock_dispatch.await_count >= 1, "dispatch_agent was never awaited"
    return mock_dispatch.await_args.kwargs["resolved_vars"]


async def test_survey_due_true_until_recorded_then_false(client, mock_dispatch, session_factory):
    contact_id = await _make_contact(session_factory)

    # No survey this month -> the dispatched call carries survey_due="true".
    _enqueue_call(client, contact_id)
    assert _last_resolved_vars(mock_dispatch)["survey_due"] == "true"

    # Record this month's survey via the tool.
    call_id = _enqueue_call(client, contact_id)
    r = client.post(
        "/v1/tools/record_survey",
        json={"call_id": call_id, "loneliness": 2, "mood": 3, "satisfaction": 4},
        headers=_auth(call_id),
    )
    assert r.status_code == 200, r.text

    # A subsequent dispatch this month no longer flags the survey as due.
    _enqueue_call(client, contact_id)
    assert _last_resolved_vars(mock_dispatch)["survey_due"] == ""


async def test_non_repeating_activity_sequence_across_calls(client, mock_dispatch, session_factory):
    from usan_api import activities_catalog

    total = len(activities_catalog.list_activities("any"))
    contact_id = await _make_contact(session_factory)

    keys: list[str] = []
    for _ in range(total):
        call_id = _enqueue_call(client, contact_id)
        r = client.post(
            "/v1/tools/get_activity",
            json={"call_id": call_id},
            headers=_auth(call_id),
        )
        assert r.status_code == 200, r.text
        keys.append(r.json()["activity_key"])

    # A full, gap-free non-repeating cycle across independent calls (SC-009).
    assert len(set(keys)) == total
