"""T066 (US7): inbound SMS opt-out (STOP) handling.

An inbound SMS whose body is a carrier opt-out keyword (STOP/STOPALL/UNSUBSCRIBE/
CANCEL/END/QUIT) is intercepted BEFORE family-task intake: the sender's number is added
to the do-not-call list (FR-038) and — when the sender is a known elder — a one-time
PHI-free opt-out acknowledgement is enqueued. A keyword never creates a family task; a
non-keyword message still routes to intake unchanged (US2 regression guard).
"""

import base64
import json
import time
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.models import DNCEntry, SmsMessage
from usan_api.settings import get_settings
from usan_api.telnyx_inbound import is_opt_out_keyword


@pytest.fixture
def signer(monkeypatch):
    """Generate an Ed25519 keypair, publish the public key to settings, return a signer."""
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


def _sender() -> str:
    return f"+1555{str(uuid.uuid4().int)[:7]}"


def _envelope(message_id: str, text_body: str, sender: str) -> bytes:
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


def _post(client, signer, raw: bytes, *, ts: str | None = None):
    ts = ts or str(int(time.time()))
    return client.post(
        "/webhooks/telnyx",
        content=raw,
        headers={
            "telnyx-signature-ed25519": signer(raw, ts),
            "telnyx-timestamp": ts,
            "Content-Type": "application/json",
        },
    )


async def _make_elder(session_factory, *, phone: str) -> str:
    async with session_factory() as db:
        elder_id = (
            await db.execute(
                text(
                    "INSERT INTO elders (name, phone_e164, timezone) "
                    "VALUES ('Ada', :p, 'UTC') RETURNING id"
                ),
                {"p": phone},
            )
        ).scalar_one()
        await db.commit()
        return str(elder_id)


async def _make_contact_for_new_elder(session_factory, *, phone: str) -> str:
    """A family contact whose number is ``phone``, linked to a fresh elder."""
    async with session_factory() as db:
        elder_id = (
            await db.execute(
                text(
                    "INSERT INTO elders (name, phone_e164, timezone) "
                    "VALUES ('Ada', :p, 'UTC') RETURNING id"
                ),
                {"p": _sender()},
            )
        ).scalar_one()
        await db.execute(
            text(
                "INSERT INTO family_contacts (elder_id, name, phone_e164) VALUES (:e, 'Dana', :p)"
            ),
            {"e": elder_id, "p": phone},
        )
        await db.commit()
        return str(elder_id)


async def _dnc_blocked(session_factory, phone: str) -> bool:
    async with session_factory() as db:
        return (await db.get(DNCEntry, phone)) is not None


async def _acks_to(session_factory, phone: str) -> list[SmsMessage]:
    async with session_factory() as db:
        rows = (
            (
                await db.execute(
                    select(SmsMessage).where(
                        SmsMessage.kind == "opt_out_ack", SmsMessage.to_number == phone
                    )
                )
            )
            .scalars()
            .all()
        )
        return list(rows)


async def _tasks_for(session_factory, elder_id: str) -> list:
    async with session_factory() as db:
        rows = await db.execute(
            text("SELECT message FROM family_tasks WHERE elder_id = :e"),
            {"e": uuid.UUID(elder_id)},
        )
        return rows.all()


# --- is_opt_out_keyword unit cases ------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("STOP", True),
        ("stop", True),
        ("  Stop  ", True),
        ("STOP.", True),
        ("stop!", True),
        ("STOPALL", True),
        ("unsubscribe", True),
        ("Cancel", True),
        ("END", True),
        ("quit", True),
        ("stop by the store", False),
        ("stopping by", False),
        ("please call me", False),
        ("", False),
    ],
)
def test_is_opt_out_keyword(body: str, expected: bool):
    assert is_opt_out_keyword(body) is expected


# --- webhook behavior --------------------------------------------------------


async def test_inbound_stop_from_elder_adds_dnc_and_ack(client, signer, session_factory):
    phone = _sender()
    await _make_elder(session_factory, phone=phone)
    r = _post(client, signer, _envelope("stop1", "STOP", phone))
    assert r.status_code == 200, r.text

    assert await _dnc_blocked(session_factory, phone) is True
    acks = await _acks_to(session_factory, phone)
    assert len(acks) == 1
    ack = acks[0]
    assert ack.call_id is None
    assert ack.status == "pending"
    low = ack.body.lower()
    for term in ("mood", "pain", "medication", "lonely"):
        assert term not in low


@pytest.mark.parametrize("keyword", ["STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"])
async def test_inbound_each_keyword_adds_dnc(client, signer, session_factory, keyword):
    phone = _sender()
    await _make_elder(session_factory, phone=phone)
    r = _post(client, signer, _envelope(f"kw_{keyword}", keyword, phone))
    assert r.status_code == 200, r.text
    assert await _dnc_blocked(session_factory, phone) is True


async def test_inbound_stop_from_known_contact_creates_no_task(client, signer, session_factory):
    # A STOP from a known family contact's number is an opt-out, not a task to relay.
    phone = _sender()
    elder_id = await _make_contact_for_new_elder(session_factory, phone=phone)
    r = _post(client, signer, _envelope("stop_contact", "STOP", phone))
    assert r.status_code == 200, r.text
    assert await _dnc_blocked(session_factory, phone) is True
    assert await _tasks_for(session_factory, elder_id) == []


async def test_inbound_non_keyword_still_creates_task(client, signer, session_factory):
    # Regression: a normal message from a known contact still routes to family intake.
    phone = _sender()
    elder_id = await _make_contact_for_new_elder(session_factory, phone=phone)
    r = _post(client, signer, _envelope("normal", "remind mom to drink water", phone))
    assert r.status_code == 200, r.text
    assert await _dnc_blocked(session_factory, phone) is False
    rows = await _tasks_for(session_factory, elder_id)
    assert len(rows) == 1


async def test_inbound_stop_unknown_sender_adds_dnc_without_ack(client, signer, session_factory):
    # STOP from a number we don't recognize still suppresses it (compliance), but there
    # is no elder to attach an acknowledgement to.
    phone = _sender()
    r = _post(client, signer, _envelope("stop_unknown", "STOP", phone))
    assert r.status_code == 200, r.text
    assert await _dnc_blocked(session_factory, phone) is True
    assert await _acks_to(session_factory, phone) == []


async def test_inbound_stop_idempotent_on_redelivery(client, signer, session_factory):
    phone = _sender()
    await _make_elder(session_factory, phone=phone)
    raw = _envelope("stop_dup", "STOP", phone)
    assert _post(client, signer, raw).status_code == 200
    assert _post(client, signer, raw).status_code == 200  # redelivery
    assert len(await _acks_to(session_factory, phone)) == 1
