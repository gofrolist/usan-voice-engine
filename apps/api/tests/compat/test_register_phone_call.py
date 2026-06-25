"""Frozen: POST /v2/register-phone-call — registered status, no auto-dial.

Tests:
1. DB-level: a REGISTERED call is never claimed by the outbound poller.
2. Route: creates a call with call_status="registered", conforms to V2PhoneCallResponse,
   and never triggers dispatch_agent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip
from tests.compat.conftest import _published_agent_id
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import contacts as contacts_repo

pytestmark = pytest.mark.frozen

NOW = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# DB-level: poller exclusion
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
async def _truncate_calls(session_factory):
    from sqlalchemy import text

    async with session_factory() as db:
        await db.execute(text("TRUNCATE calls CASCADE"))
        await db.commit()


async def _seed_registered(factory) -> uuid.UUID:
    """Insert a REGISTERED call with scheduled_at in the past."""
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        contact = await contacts_repo.create_contact(
            db, name="Test", phone_e164=phone, timezone="UTC"
        )
        call = Call(
            contact_id=contact.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.REGISTERED,
            scheduled_at=NOW - timedelta(minutes=5),
        )
        db.add(call)
        await db.flush()
        await db.commit()
        return call.id


@pytest.mark.asyncio
async def test_poller_never_claims_registered(session_factory):
    """A REGISTERED row must never be claimed by claim_due_retries (which filters QUEUED only)."""
    await _seed_registered(session_factory)
    async with session_factory() as db:
        claimed = await calls_repo.claim_due_retries(db, now=NOW + timedelta(hours=1), limit=10)
    assert claimed == [], (
        "claim_due_retries must never return a REGISTERED call; "
        "only QUEUED rows are eligible for auto-dial"
    )


# ---------------------------------------------------------------------------
# Route: conformance + no-dial
# ---------------------------------------------------------------------------


def test_register_creates_registered_call_and_conforms(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    agent_id = _published_agent_id(compat_client, compat_headers)
    resp = compat_client.post(
        "/v2/register-phone-call",
        json={
            "agent_id": agent_id,
            "direction": "outbound",
            "from_number": "+15551230000",
            "to_number": "+15557654321",
        },
        headers=compat_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["call_status"] == "registered"
    assert body["agent_id"]
    assert body["agent_version"] is not None
    assert_conforms(body, "V2PhoneCallResponse")
    assert_sdk_roundtrip(body, "retell.types:PhoneCallResponse")
    mock_dispatch.assert_not_awaited()  # registered != dialed


def test_register_unknown_agent_is_422(compat_client, compat_headers):
    """Passing an agent_id that doesn't exist (or is unpublished) must return 422."""
    resp = compat_client.post(
        "/v2/register-phone-call",
        json={"agent_id": "agent_doesnotexist000000000000000000"},
        headers=compat_headers,
    )
    assert resp.status_code == 422, resp.text


def test_register_missing_agent_id_is_422(compat_client, compat_headers):
    """agent_id is required; omitting it must return 422 (Pydantic validation)."""
    resp = compat_client.post(
        "/v2/register-phone-call",
        json={"from_number": "+15551230000", "to_number": "+15557654321"},
        headers=compat_headers,
    )
    assert resp.status_code == 422, resp.text
