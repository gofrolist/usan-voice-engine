"""Inbound two-way SMS reply engine via POST /webhooks/telnyx (Phase 4b-2)."""

from __future__ import annotations

import base64
import json
import time
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import telnyx_messaging
from usan_api.compat import sms_reply
from usan_api.db.base import ChatStatus, ProfileStatus
from usan_api.db.models import AgentProfile, ChatSession
from usan_api.repositories import chats as chats_repo
from usan_api.settings import get_settings

_OUR = "+15550000000"
_RECIP = "+15551234567"


@pytest.fixture
def signer(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    monkeypatch.setenv("TELNYX_INBOUND_PUBLIC_KEY", pub_b64)
    monkeypatch.setenv("TELNYX_INBOUND_SMS_REPLY_ENABLED", "true")
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
def disabled_signer(monkeypatch):
    priv = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    monkeypatch.setenv("TELNYX_INBOUND_PUBLIC_KEY", pub_b64)
    monkeypatch.delenv("TELNYX_INBOUND_SMS_REPLY_ENABLED", raising=False)
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
    """Patch the shared Vertex helper where the engine looks it up (no real Vertex / config)."""

    async def _gen(db, settings, session):
        return "Thanks, noted!"

    monkeypatch.setattr(sms_reply, "generate_agent_reply", _gen)


@pytest.fixture
def recorded_sms(monkeypatch):
    calls: list[dict[str, str]] = []

    async def _send(settings, *, to_number: str, body: str) -> str:
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


async def _seed_open_sms_chat(factory, *, frm=_OUR, to=_RECIP) -> uuid.UUID:
    async with factory() as db:
        profile = AgentProfile(
            name=f"A {uuid.uuid4().hex[:8]}",
            draft_config={"general_prompt": "x"},
            status=ProfileStatus.ACTIVE,
            published_version=1,
        )
        db.add(profile)
        await db.flush()
        s = ChatSession(
            agent_profile_id=profile.id,
            agent_version=1,
            chat_type="sms_chat",
            dynamic_vars={},
            from_number=frm,
            to_number=to,
            status=ChatStatus.ONGOING,
        )
        db.add(s)
        await db.commit()
        return s.id


async def _messages(factory, session_id):
    async with factory() as db:
        return await chats_repo.list_messages(db, session_id)


@pytest.mark.asyncio
async def test_matched_inbound_generates_and_sends_reply(
    client, signer, fake_reply, recorded_sms, session_factory
):
    sid = await _seed_open_sms_chat(session_factory)
    r = _post(client, signer, _envelope("m1", "hi back", sender=_RECIP, recipient=_OUR))
    assert r.status_code == 200, r.text
    assert len(recorded_sms) == 1
    assert recorded_sms[0]["to_number"] == _RECIP
    assert recorded_sms[0]["body"] == "Thanks, noted!"
    msgs = await _messages(session_factory, sid)
    roles = [(m.role, m.provider_message_id) for m in msgs]
    assert ("sms", "m1") in roles
    assert any(role == "agent" and pid is None for role, pid in roles)


@pytest.mark.asyncio
async def test_redelivery_is_deduped(client, signer, fake_reply, recorded_sms, session_factory):
    await _seed_open_sms_chat(session_factory)
    raw = _envelope("dup1", "hi", sender=_RECIP, recipient=_OUR)
    assert _post(client, signer, raw).status_code == 200
    assert _post(client, signer, raw).status_code == 200  # redelivery
    assert len(recorded_sms) == 1  # only one reply sent


@pytest.mark.asyncio
async def test_no_session_falls_through(client, signer, fake_reply, recorded_sms, session_factory):
    # no open sms_chat seeded -> engine returns False -> family-task path (no match) -> 200
    r = _post(client, signer, _envelope("m2", "hello", sender=_RECIP, recipient=_OUR))
    assert r.status_code == 200, r.text
    assert recorded_sms == []


@pytest.mark.asyncio
async def test_disabled_flag_does_not_reply(client, disabled_signer, recorded_sms, session_factory):
    await _seed_open_sms_chat(session_factory)
    r = _post(client, disabled_signer, _envelope("m3", "hi", sender=_RECIP, recipient=_OUR))
    assert r.status_code == 200, r.text
    assert recorded_sms == []  # flag off -> no reply


@pytest.mark.asyncio
async def test_send_failure_rolls_back_no_orphan(
    client, signer, fake_reply, monkeypatch, session_factory
):
    sid = await _seed_open_sms_chat(session_factory)

    async def _boom(settings, *, to_number, body):
        raise telnyx_messaging.TelnyxMessagingError("boom")

    monkeypatch.setattr(telnyx_messaging, "send_sms", _boom)
    r = _post(client, signer, _envelope("m4", "hi", sender=_RECIP, recipient=_OUR))
    assert r.status_code == 200, r.text
    msgs = await _messages(session_factory, sid)
    assert all(m.provider_message_id != "m4" for m in msgs)  # inbound rolled back
    assert all(m.role != "agent" for m in msgs)  # no reply persisted


@pytest.mark.asyncio
async def test_stop_keyword_still_opts_out_not_replies(
    client, signer, fake_reply, recorded_sms, session_factory
):
    await _seed_open_sms_chat(session_factory)
    r = _post(client, signer, _envelope("m5", "STOP", sender=_RECIP, recipient=_OUR))
    assert r.status_code == 200, r.text
    assert recorded_sms == []  # opt-out wins; no chat reply
