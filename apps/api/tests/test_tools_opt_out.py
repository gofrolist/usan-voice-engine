"""T065 (US7): register_opt_out tool contract.

A call-scoped ``register_opt_out`` honors a spoken opt-out (FR-037): it adds the contact's
number to the do-not-call list (so future outbound is suppressed — SC-010), enqueues a
one-time PHI-free opt-out acknowledgement SMS to the contact, and raises a routine
``operator_alert`` follow-up flag so a human understands why calls stopped (FR-039).
Idempotent within a call. Token-scope and missing-contact guards mirror the other tools.
"""

import asyncio
import time
import uuid

import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import livekit_dispatch
from usan_api.db.models import DNCEntry, FollowUpFlag, SmsMessage

_OP = {"Authorization": "Bearer " + "o" * 32}


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


def _service_token(call_id: str, secret: str = "s" * 32) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )


def _worker_token(secret: str = "s" * 32) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "iat": now, "exp": now + 300}, secret, algorithm="HS256"
    )


def _auth(call_id: str) -> dict:
    return {"Authorization": f"Bearer {_service_token(call_id)}"}


def _phone() -> str:
    return f"+1555{str(uuid.uuid4().int)[:7]}"


def _create_contact(client, phone: str) -> str:
    r = client.post(
        "/v1/contacts",
        json={"name": "Ada", "phone_e164": phone, "timezone": "UTC", "metadata": {}},
        headers=_OP,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _enqueue(client, contact_id: str) -> dict:
    r = client.post(
        "/v1/calls",
        json={
            "contact_id": contact_id,
            "idempotency_key": f"oo-{uuid.uuid4()}",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    assert r.status_code == 202, r.text
    return r.json()


def _query(url, coro_factory):
    async def _do():
        engine = create_async_engine(url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                return await coro_factory(db)
        finally:
            await engine.dispose()

    return asyncio.run(_do())


def _dnc_entry(url, phone: str) -> DNCEntry | None:
    return _query(url, lambda db: db.get(DNCEntry, phone))


def _opt_out_acks(url, to_number: str) -> list[SmsMessage]:
    async def _q(db):
        rows = (
            (
                await db.execute(
                    select(SmsMessage).where(
                        SmsMessage.kind == "opt_out_ack",
                        SmsMessage.to_number == to_number,
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    return _query(url, _q)


def _operator_flags(url, call_id: str) -> list[FollowUpFlag]:
    async def _q(db):
        rows = (
            (
                await db.execute(
                    select(FollowUpFlag).where(
                        FollowUpFlag.call_id == uuid.UUID(call_id),
                        FollowUpFlag.category == "operator_alert",
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    return _query(url, _q)


def test_register_opt_out_adds_dnc_acks_and_flags(client, mock_dispatch, async_database_url):
    phone = _phone()
    contact_id = _create_contact(client, phone)
    call_id = _enqueue(client, contact_id)["id"]

    r = client.post(
        "/v1/tools/register_opt_out",
        json={"call_id": call_id},
        headers=_auth(call_id),
    )
    assert r.status_code == 200, r.text

    # DNC entry created for the contact's number (SC-010: no further outbound).
    entry = _dnc_entry(async_database_url, phone)
    assert entry is not None
    assert entry.reason  # an audit reason is recorded

    # Exactly one PHI-free opt-out ack to the contact (call_id IS NULL — outbox-delivered).
    acks = _opt_out_acks(async_database_url, phone)
    assert len(acks) == 1
    ack = acks[0]
    assert ack.call_id is None
    assert ack.status == "pending"
    low = ack.body.lower()
    for term in ("mood", "pain", "medication", "lonely", "satisfaction"):
        assert term not in low

    # Operator queue entry so a human understands why calls stopped (FR-039).
    flags = _operator_flags(async_database_url, call_id)
    assert len(flags) == 1
    assert flags[0].severity == "routine"


def test_register_opt_out_is_idempotent_within_call(client, mock_dispatch, async_database_url):
    phone = _phone()
    contact_id = _create_contact(client, phone)
    call_id = _enqueue(client, contact_id)["id"]

    assert (
        client.post(
            "/v1/tools/register_opt_out", json={"call_id": call_id}, headers=_auth(call_id)
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/v1/tools/register_opt_out", json={"call_id": call_id}, headers=_auth(call_id)
        ).status_code
        == 200
    )

    # A repeat in the same call does not double-text or pile up operator flags.
    assert len(_opt_out_acks(async_database_url, phone)) == 1
    assert len(_operator_flags(async_database_url, call_id)) == 1


def test_register_opt_out_suppresses_future_outbound(client, mock_dispatch, async_database_url):
    # SC-010: once opted out, a later outbound enqueue is terminal-at-birth DNC_BLOCKED.
    phone = _phone()
    contact_id = _create_contact(client, phone)
    call_id = _enqueue(client, contact_id)["id"]
    client.post("/v1/tools/register_opt_out", json={"call_id": call_id}, headers=_auth(call_id))

    # A DNC-blocked enqueue is terminal at birth: the call is created with status
    # dnc_blocked and never dialed (the response is 200, not the 202 of a queued dial).
    later = client.post(
        "/v1/calls",
        json={
            "contact_id": contact_id,
            "idempotency_key": f"oo-{uuid.uuid4()}",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    assert later.status_code == 200, later.text
    assert later.json()["status"] == "dnc_blocked"


def test_register_opt_out_rejects_wrong_call_token(client, mock_dispatch, async_database_url):
    phone = _phone()
    contact_id = _create_contact(client, phone)
    call_id = _enqueue(client, contact_id)["id"]
    other = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/register_opt_out",
        json={"call_id": call_id},
        headers=_auth(other),  # token scoped to a different call
    )
    assert r.status_code == 403


def test_register_opt_out_409_when_call_has_no_contact(client, mock_dispatch):
    # An inbound call from an unknown number has no contact; the tool 409s rather than
    # guessing whose number to suppress.
    inbound = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": "+19990008888", "livekit_room": f"oo-{uuid.uuid4()}"},
        headers={"Authorization": f"Bearer {_worker_token()}"},
    ).json()
    call_id = inbound["call_id"]
    r = client.post(
        "/v1/tools/register_opt_out",
        json={"call_id": call_id},
        headers=_auth(call_id),
    )
    assert r.status_code == 409
