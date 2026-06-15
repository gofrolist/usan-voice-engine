"""T042 (US4): POST /v1/tools/record_personal_fact captures a durable contact fact.

Contract (contracts/tools-api.md): the agent calls this when the contact states a
durable fact. It inserts a ``personal_facts`` row with ``source='contact_stated'`` and
returns ``{"id": <int>}``. The request mirrors every other tool: a JWT scoped to the
call resolves the contact; ``category`` is a closed set; ``structured`` is optional.

Written FIRST (Constitution IV) — fails until the schema + repo + endpoint land.
"""

import time
import uuid

import jwt
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import livekit_dispatch

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


async def _make_contact(session_factory) -> str:
    async with session_factory() as db:
        eid = (
            await db.execute(
                text(
                    "INSERT INTO contacts (name, phone_e164, timezone) "
                    "VALUES ('Ada', :p, 'UTC') RETURNING id"
                ),
                {"p": f"+1555{str(uuid.uuid4().int)[:7]}"},
            )
        ).scalar_one()
        await db.commit()
        return str(eid)


async def _facts(session_factory, contact_id: str):
    async with session_factory() as db:
        return (
            await db.execute(
                text(
                    "SELECT category, content, structured, source, active, phi "
                    "FROM personal_facts WHERE contact_id = :e ORDER BY id"
                ),
                {"e": contact_id},
            )
        ).all()


def _enqueue_call(client, contact_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={
            "contact_id": contact_id,
            "idempotency_key": f"pf-{uuid.uuid4()}",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


def _record(client, call_id: str, **body):
    return client.post(
        "/v1/tools/record_personal_fact",
        json={"call_id": call_id, **body},
        headers=_auth(call_id),
    )


async def test_records_contact_stated_fact(client, mock_dispatch, session_factory):
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)

    r = _record(client, call_id, category="person", content="daughter Maria visits on Sundays")
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["id"], int)

    rows = await _facts(session_factory, contact_id)
    assert len(rows) == 1
    assert rows[0].category == "person"
    assert rows[0].content == "daughter Maria visits on Sundays"
    assert rows[0].source == "contact_stated"  # the tool never writes 'extracted'/'operator'
    assert rows[0].active is True
    assert rows[0].structured == {}  # omitted -> default empty object


async def test_records_structured_payload(client, mock_dispatch, session_factory):
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)

    r = _record(
        client,
        call_id,
        category="important_date",
        content="birthday",
        structured={"date": "2026-07-04", "label": "birthday"},
    )
    assert r.status_code == 200, r.text
    rows = await _facts(session_factory, contact_id)
    assert rows[0].structured == {"date": "2026-07-04", "label": "birthday"}


async def test_rejects_unknown_category(client, mock_dispatch, session_factory):
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)
    # category is a closed set — an off-enum value never reaches the DB.
    r = _record(client, call_id, category="bank_pin", content="1234")
    assert r.status_code == 422, r.text
    assert await _facts(session_factory, contact_id) == []


async def test_health_context_defaults_phi_true(client, mock_dispatch, session_factory):
    # health_context is clinical: it MUST persist phi=true (data-model) so the PHI
    # governance/retention layer can treat it as protected.
    contact_id = await _make_contact(session_factory)
    call_id = _enqueue_call(client, contact_id)
    r = _record(client, call_id, category="health_context", content="uses a walker indoors")
    assert r.status_code == 200, r.text
    rows = await _facts(session_factory, contact_id)
    assert rows[0].phi is True


async def test_token_scoped_to_other_call_is_rejected(client, mock_dispatch, session_factory):
    # Same contact-scoping guard as every other tool: a JWT minted for call A cannot
    # write a fact under a body claiming call B (403 from _authorize_call).
    contact_id = await _make_contact(session_factory)
    call_a = _enqueue_call(client, contact_id)
    call_b = _enqueue_call(client, contact_id)
    r = client.post(
        "/v1/tools/record_personal_fact",
        json={"call_id": call_b, "category": "preference", "content": "likes tea"},
        headers=_auth(call_a),
    )
    assert r.status_code == 403, r.text
    assert await _facts(session_factory, contact_id) == []
