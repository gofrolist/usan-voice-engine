"""T023 / T031 (US2): family-task loop + close_family_task tool.

The end-to-end loop: a family contact texts a task (signed inbound webhook → an open
``family_tasks`` row), it is conveyed on the next call (it appears in
``list_open_family_tasks``, the source of the ``open_family_tasks`` builtin), the agent
marks it delivered via ``POST /v1/tools/close_family_task`` (open → delivered, stamping
``delivered_call_id``), and a following call never repeats it. Also covers the tool's
explicit-task_id path, its cross-contact scope guard, and the empty no-op.
"""

import base64
import json
import time
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import OPERATOR_HEADERS as _OP
from tests.conftest import service_token as _service_token
from usan_api import livekit_dispatch
from usan_api.settings import get_settings

_SENDER = "+15550009001"


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


@pytest.fixture
def signer(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    monkeypatch.setenv("TELNYX_INBOUND_PUBLIC_KEY", pub_b64)
    get_settings.cache_clear()

    def _sign(raw: bytes, ts: str) -> str:
        return base64.b64encode(priv.sign(f"{ts}|".encode() + raw)).decode()

    return _sign


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _auth(call_id: str) -> dict:
    return {"Authorization": f"Bearer {_service_token(call_id)}"}


def _envelope(message_id: str, text_body: str, sender: str = _SENDER) -> bytes:
    return json.dumps(
        {
            "data": {
                "event_type": "message.received",
                "id": f"evt_{message_id}",
                "payload": {
                    "id": message_id,
                    "from": {"phone_number": sender},
                    "text": text_body,
                },
            }
        }
    ).encode()


def _post_sms(client, signer, raw: bytes):
    ts = str(int(time.time()))
    return client.post(
        "/webhooks/telnyx",
        content=raw,
        headers={
            "telnyx-signature-ed25519": signer(raw, ts),
            "telnyx-timestamp": ts,
            "Content-Type": "application/json",
        },
    )


def _intake(client, signer, message_id: str, text_body: str, sender: str = _SENDER) -> None:
    r = _post_sms(client, signer, _envelope(message_id, text_body, sender))
    assert r.status_code == 200, r.text


async def _make_contact_and_contact(session_factory, *, phone: str = _SENDER) -> str:
    async with session_factory() as db:
        contact_id = (
            await db.execute(
                text(
                    "INSERT INTO contacts (name, phone_e164, timezone) "
                    "VALUES ('Ada', :p, 'UTC') RETURNING id"
                ),
                {"p": f"+1555{str(uuid.uuid4().int)[:7]}"},
            )
        ).scalar_one()
        await db.execute(
            text(
                "INSERT INTO family_contacts (contact_id, name, phone_e164) VALUES (:e, 'Dana', :p)"
            ),
            {"e": str(contact_id), "p": phone},
        )
        await db.commit()
        return str(contact_id)


async def _open_tasks(session_factory, contact_id: str):
    async with session_factory() as db:
        rows = await db.execute(
            text(
                "SELECT id, message, status, delivered_call_id FROM family_tasks "
                "WHERE contact_id = :e AND status = 'open' AND needs_safety_review IS FALSE "
                "ORDER BY created_at, id"
            ),
            {"e": contact_id},
        )
        return rows.all()


async def _task_row(session_factory, task_id: int):
    async with session_factory() as db:
        return (
            await db.execute(
                text("SELECT status, delivered_call_id FROM family_tasks WHERE id = :i"),
                {"i": task_id},
            )
        ).one()


def _enqueue_call(client, contact_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={
            "contact_id": contact_id,
            "idempotency_key": f"ft-{uuid.uuid4()}",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


async def test_family_task_loop_intake_convey_close_no_repeat(
    client, mock_dispatch, signer, session_factory
):
    # 1. Intake: family contact texts a task -> one open family_task.
    contact_id = await _make_contact_and_contact(session_factory)
    _intake(client, signer, "m1", "remind mom to drink water")

    # 2. Convey: the open task is what the next call injects (open_family_tasks builtin).
    conveyed = await _open_tasks(session_factory, contact_id)
    assert [r.message for r in conveyed] == ["remind mom to drink water"]
    task_id = conveyed[0].id

    # 3. Close: the agent marks it delivered after conveying (no task_id -> close all).
    call_id = _enqueue_call(client, contact_id)
    r = client.post(
        "/v1/tools/close_family_task", json={"call_id": call_id}, headers=_auth(call_id)
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "delivered", "delivered": 1}

    delivered = await _task_row(session_factory, task_id)
    assert delivered.status == "delivered"
    assert str(delivered.delivered_call_id) == call_id

    # 4. No repeat: a following call sees no open tasks.
    assert await _open_tasks(session_factory, contact_id) == []


async def test_close_family_task_explicit_task_id(client, mock_dispatch, signer, session_factory):
    contact_id = await _make_contact_and_contact(session_factory)
    _intake(client, signer, "m2", "ask about her doctor visit")
    task_id = (await _open_tasks(session_factory, contact_id))[0].id

    call_id = _enqueue_call(client, contact_id)
    r = client.post(
        "/v1/tools/close_family_task",
        json={"call_id": call_id, "task_id": task_id},
        headers=_auth(call_id),
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "delivered", "delivered": 1}
    assert (await _task_row(session_factory, task_id)).status == "delivered"


async def test_close_family_task_cross_contact_task_returns_404(
    client, mock_dispatch, signer, session_factory
):
    # A call may only deliver ITS OWN contact's tasks. Contact A owns the task; contact B's call
    # must not be able to close it (scope guard -> 404, task untouched).
    contact_a = await _make_contact_and_contact(session_factory, phone="+15550000001")
    _intake(client, signer, "m3", "water the plants", sender="+15550000001")
    task_id = (await _open_tasks(session_factory, contact_a))[0].id

    contact_b = await _make_contact_and_contact(session_factory, phone="+15550000002")
    call_b = _enqueue_call(client, contact_b)
    r = client.post(
        "/v1/tools/close_family_task",
        json={"call_id": call_b, "task_id": task_id},
        headers=_auth(call_b),
    )
    assert r.status_code == 404
    assert (await _task_row(session_factory, task_id)).status == "open"  # untouched


async def test_close_family_task_noop_when_no_open_tasks(client, mock_dispatch, session_factory):
    contact_id = await _make_contact_and_contact(session_factory)  # contact, but no task texted
    call_id = _enqueue_call(client, contact_id)
    r = client.post(
        "/v1/tools/close_family_task", json={"call_id": call_id}, headers=_auth(call_id)
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "noop", "delivered": 0}
