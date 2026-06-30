"""Unknown-recipient inbound SMS auto-create via POST /webhooks/telnyx (Phase 4b-3)."""

from __future__ import annotations

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

from usan_api import telnyx_messaging
from usan_api.compat import ids, inbound_autocreate
from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, PhoneNumber
from usan_api.settings import get_settings

_OUR = "+15550000000"
_SENDER = "+15551234567"


@pytest.fixture
def signer(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    monkeypatch.setenv("TELNYX_INBOUND_PUBLIC_KEY", pub_b64)
    monkeypatch.setenv("TELNYX_INBOUND_SMS_AUTOCREATE_ENABLED", "true")
    monkeypatch.setenv("TELNYX_MESSAGING_ENABLED", "true")
    monkeypatch.setenv("TELNYX_MESSAGING_API_KEY", "test-key")
    monkeypatch.setenv("TELNYX_MESSAGING_PROFILE_ID", "test-profile")
    monkeypatch.setenv("TELNYX_FROM_NUMBER", _OUR)
    monkeypatch.setenv("GCP_PROJECT", "test-project")
    get_settings.cache_clear()

    def _sign(raw: bytes, ts: str) -> str:
        return base64.b64encode(priv.sign(f"{ts}|".encode() + raw)).decode()

    return _sign


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def fake_reply(monkeypatch):
    async def _gen(db, settings, session):
        return "Thanks, noted!"

    monkeypatch.setattr(inbound_autocreate, "generate_agent_reply", _gen)


@pytest.fixture
def recorded_sms(monkeypatch):
    calls: list[dict[str, str]] = []

    async def _send(settings, *, to_number, body):
        calls.append({"to_number": to_number, "body": body})
        return "tx-out"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _send)
    return calls


def _envelope(message_id, text_body, *, sender, recipient):
    return json.dumps(
        {
            "data": {
                "event_type": "message.received",
                "id": f"evt_{message_id}",
                "payload": {
                    "id": message_id,
                    "from": {"phone_number": sender},
                    "to": [{"phone_number": recipient}],
                    "text": text_body,
                },
            }
        }
    ).encode()


def _post(client, signer, raw, *, ts=None):
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


async def _seed_bound_number(factory) -> None:
    async with factory() as db:
        profile = AgentProfile(
            name=f"A {uuid.uuid4().hex[:8]}",
            draft_config={"general_prompt": "x"},
            status=ProfileStatus.ACTIVE,
            published_version=1,
        )
        db.add(profile)
        await db.flush()
        db.add(
            PhoneNumber(
                phone_e164=_OUR,
                phone_number_type="custom",
                inbound_sms_agents=[{"agent_id": ids.encode_agent_id(profile.id), "weight": 1.0}],
            )
        )
        await db.commit()


async def _session_count(factory) -> int:
    async with factory() as db:
        return int(
            (
                await db.execute(
                    text("SELECT count(*) FROM chat_sessions WHERE to_number = :sender"),
                    {"sender": _SENDER},
                )
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_unknown_sender_autocreates_and_replies(
    client, signer, fake_reply, recorded_sms, session_factory
):
    await _seed_bound_number(session_factory)
    r = _post(client, signer, _envelope("m1", "hi", sender=_SENDER, recipient=_OUR))
    assert r.status_code == 200, r.text
    assert recorded_sms == [{"to_number": _SENDER, "body": "Thanks, noted!"}]
    assert await _session_count(session_factory) == 1


@pytest.mark.asyncio
async def test_stop_keyword_wins_over_autocreate(
    client, signer, fake_reply, recorded_sms, session_factory
):
    await _seed_bound_number(session_factory)
    r = _post(client, signer, _envelope("m2", "STOP", sender=_SENDER, recipient=_OUR))
    assert r.status_code == 200, r.text
    assert recorded_sms == []  # opt-out first; no chat
    assert await _session_count(session_factory) == 0


@pytest.mark.asyncio
async def test_no_binding_falls_through_to_family_task(
    client, signer, fake_reply, recorded_sms, session_factory
):
    # No phone_number seeded -> Gate 1 declines -> family-task path (unmatched) -> 200, no chat.
    r = _post(client, signer, _envelope("m3", "hello", sender=_SENDER, recipient=_OUR))
    assert r.status_code == 200, r.text
    assert recorded_sms == []
    assert await _session_count(session_factory) == 0


@pytest.mark.asyncio
async def test_redelivery_deduped_one_chat(
    client, signer, fake_reply, recorded_sms, session_factory
):
    await _seed_bound_number(session_factory)
    raw = _envelope("dup", "hi", sender=_SENDER, recipient=_OUR)
    assert _post(client, signer, raw).status_code == 200
    # Redelivery of the same message_id: the second delivery finds the now-open chat at Gate 0
    # and declines — one chat, one reply.
    assert _post(client, signer, raw).status_code == 200
    assert await _session_count(session_factory) == 1
    assert len(recorded_sms) == 1


@pytest.mark.asyncio
async def test_multi_turn_does_not_fork_second_chat(
    client, signer, fake_reply, recorded_sms, session_factory
):
    # Reply flag OFF (signer sets only autocreate). Two DISTINCT inbound turns (different
    # message ids) from the same unknown sender to the bound DID must yield exactly ONE chat
    # and ONE reply: the second turn finds the open chat at Gate 0 and declines.
    await _seed_bound_number(session_factory)
    assert (
        _post(client, signer, _envelope("t1", "hello", sender=_SENDER, recipient=_OUR)).status_code
        == 200
    )
    assert (
        _post(client, signer, _envelope("t2", "again", sender=_SENDER, recipient=_OUR)).status_code
        == 200
    )
    assert await _session_count(session_factory) == 1
    assert len(recorded_sms) == 1
