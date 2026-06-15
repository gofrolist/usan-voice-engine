"""Inbound Telnyx webhook contract (US2 / T022).

Ed25519 signature verify (forged → 401, stale → 401), family-task intake for a known
sender, idempotency on the Telnyx message id, unmatched-sender safe default (FR-014),
and the medical-safety screen (FR-015).
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

from usan_api.settings import get_settings

_SENDER = "+15550009001"


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


async def _make_contact_and_contact(session_factory, *, phone: str = _SENDER):
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
            {"e": contact_id, "p": phone},
        )
        await db.commit()
        return contact_id


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


async def _open_tasks(session_factory, contact_id):
    async with session_factory() as db:
        rows = await db.execute(
            text(
                "SELECT message, status, needs_safety_review FROM family_tasks "
                "WHERE contact_id = :e ORDER BY id"
            ),
            {"e": contact_id},
        )
        return rows.all()


async def test_known_contact_creates_open_task(client, signer, session_factory):
    contact_id = await _make_contact_and_contact(session_factory)
    raw = _envelope("msg_1", "remind mom to drink water")
    r = _post(client, signer, raw)
    assert r.status_code == 200, r.text

    rows = await _open_tasks(session_factory, contact_id)
    assert len(rows) == 1
    assert rows[0].message == "remind mom to drink water"
    assert rows[0].status == "open"
    assert rows[0].needs_safety_review is False


async def test_idempotent_on_message_id(client, signer, session_factory):
    contact_id = await _make_contact_and_contact(session_factory)
    raw = _envelope("msg_dup", "remind mom to drink water")
    assert _post(client, signer, raw).status_code == 200
    assert _post(client, signer, raw).status_code == 200  # redelivery
    rows = await _open_tasks(session_factory, contact_id)
    assert len(rows) == 1  # not duplicated


async def test_forged_signature_rejected_401(client, signer, session_factory):
    await _make_contact_and_contact(session_factory)
    raw = _envelope("msg_forge", "hello")
    ts = str(int(time.time()))
    r = client.post(
        "/webhooks/telnyx",
        content=raw,
        headers={
            "telnyx-signature-ed25519": base64.b64encode(b"x" * 64).decode(),
            "telnyx-timestamp": ts,
        },
    )
    assert r.status_code == 401


async def test_stale_timestamp_rejected_401(client, signer, session_factory):
    await _make_contact_and_contact(session_factory)
    raw = _envelope("msg_old", "hello")
    old_ts = str(int(time.time()) - 4000)  # beyond webhook_max_age_s (300)
    r = _post(client, signer, raw, ts=old_ts)  # validly signed over the old ts
    assert r.status_code == 401  # replay — same status as forged (no oracle)


async def test_unmatched_sender_creates_no_task(client, signer, session_factory):
    contact_id = await _make_contact_and_contact(session_factory)
    raw = _envelope("msg_unknown", "remind mom", sender="+15559999999")
    assert _post(client, signer, raw).status_code == 200
    assert await _open_tasks(session_factory, contact_id) == []  # FR-014


async def test_unsafe_task_flagged_for_review(client, signer, session_factory):
    contact_id = await _make_contact_and_contact(session_factory)
    raw = _envelope("msg_unsafe", "tell mom to stop taking her heart pills")
    assert _post(client, signer, raw).status_code == 200
    rows = await _open_tasks(session_factory, contact_id)
    assert len(rows) == 1
    assert rows[0].needs_safety_review is True  # FR-015 — not relayed verbatim


async def test_national_format_sender_matches_e164_contact(client, signer, session_factory):
    # Telnyx may deliver a bare 10-digit national number; the contact is stored E.164.
    # The intake must normalize before lookup, else a known contact silently matches
    # nothing (FR-008/014). Contact is +15550009001; sender arrives as 5550009001.
    contact_id = await _make_contact_and_contact(session_factory, phone=_SENDER)
    national = _SENDER.removeprefix("+1")  # "5550009001"
    raw = _envelope("msg_natl", "remind mom about lunch", sender=national)
    assert _post(client, signer, raw).status_code == 200
    rows = await _open_tasks(session_factory, contact_id)
    assert len(rows) == 1
    assert rows[0].message == "remind mom about lunch"


def test_inbound_sms_text_is_length_capped():
    # Security review: bound the stored inbound text so an oversized payload can't write an
    # unbounded blob into family_tasks.message.
    from usan_api.schemas.inbound_sms import _MAX_SMS_TEXT_CHARS, parse_inbound_sms

    payload = {
        "data": {
            "event_type": "message.received",
            "payload": {
                "id": "msg_big",
                "from": {"phone_number": "+15550009001"},
                "text": "x" * 5000,
            },
        }
    }
    parsed = parse_inbound_sms(payload)
    assert parsed is not None
    assert len(parsed.text) == _MAX_SMS_TEXT_CHARS
