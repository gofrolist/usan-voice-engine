"""T087 (US7 / FR-041): send_info_sms tool contract.

``send_info_sms`` lets the agent text the contact a PHI-minimized SMS of helpful phone
numbers — the public emergency/helpline numbers sourced from ``emergency_resources`` so
they never drift. Unlike ``send_sms`` it needs no operator template; it builds a fixed,
PHI-free body. It shares the per-call SMS budget and the standard token-scope/contact guards.
"""

import asyncio
import time
import uuid

import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import OPERATOR_HEADERS as _OP
from tests.conftest import service_token as _service_token
from usan_api import emergency_resources, livekit_dispatch
from usan_api.db.models import SmsMessage


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


def _worker_token(secret: str = "s" * 32) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "iat": now, "exp": now + 300}, secret, algorithm="HS256"
    )


def _auth(call_id: str) -> dict:
    return {"Authorization": f"Bearer {_service_token(call_id)}"}


def _create_contact(client) -> str:
    r = client.post(
        "/v1/contacts",
        json={
            "name": "Ada",
            "phone_e164": f"+1555{str(uuid.uuid4().int)[:7]}",
            "timezone": "UTC",
            "metadata": {},
        },
        headers=_OP,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _enqueue(client, contact_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={
            "contact_id": contact_id,
            "idempotency_key": f"info-{uuid.uuid4()}",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


def _info_rows(url, call_id: str) -> list[SmsMessage]:
    async def _do():
        engine = create_async_engine(url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                rows = (
                    (
                        await db.execute(
                            select(SmsMessage).where(
                                SmsMessage.call_id == uuid.UUID(call_id),
                                SmsMessage.template_key == "info_resources",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                return list(rows)
        finally:
            await engine.dispose()

    return asyncio.run(_do())


# --- informational_sms_body unit cases --------------------------------------


def test_informational_sms_body_lists_resource_numbers():
    body = emergency_resources.informational_sms_body()
    for number in ("911", "988", "1-800-222-1222", "1-800-677-1116"):
        assert number in body
    assert len(body) <= 480  # one SMS segment-budget bound (SmsTemplate cap)


def test_informational_sms_body_is_phi_free():
    low = emergency_resources.informational_sms_body().lower()
    for term in ("mood", "pain", "diagnos", "medication", "lonely"):
        assert term not in low


# --- tool contract -----------------------------------------------------------


def test_send_info_sms_queues_message_from_catalog(client, mock_dispatch, async_database_url):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post("/v1/tools/send_info_sms", json={"call_id": call_id}, headers=_auth(call_id))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"

    rows = _info_rows(async_database_url, call_id)
    assert len(rows) == 1
    # Body is exactly the single-source-of-truth catalog text (no drift).
    assert rows[0].body == emergency_resources.informational_sms_body()


def test_send_info_sms_respects_per_call_budget(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    for _ in range(3):  # MAX_SMS_PER_CALL
        assert (
            client.post(
                "/v1/tools/send_info_sms", json={"call_id": call_id}, headers=_auth(call_id)
            ).status_code
            == 200
        )
    r = client.post("/v1/tools/send_info_sms", json={"call_id": call_id}, headers=_auth(call_id))
    assert r.status_code == 409


def test_send_info_sms_rejects_wrong_call_token(bare_client):
    call_id = str(uuid.uuid4())
    r = bare_client.post(
        "/v1/tools/send_info_sms",
        json={"call_id": call_id},
        headers=_auth(str(uuid.uuid4())),
    )
    assert r.status_code == 403


def test_send_info_sms_409_when_call_has_no_contact(client, mock_dispatch):
    inbound = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": "+19990007777", "livekit_room": f"info-{uuid.uuid4()}"},
        headers={"Authorization": f"Bearer {_worker_token()}"},
    ).json()
    call_id = inbound["call_id"]
    r = client.post("/v1/tools/send_info_sms", json={"call_id": call_id}, headers=_auth(call_id))
    assert r.status_code == 409
