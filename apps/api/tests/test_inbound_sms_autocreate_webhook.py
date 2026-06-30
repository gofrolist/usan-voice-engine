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


# ---------------------------------------------------------------------------
# Concurrency regression: Fix #1 — advisory lock serializes concurrent first-contact
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_distinct_first_deliveries_create_one_chat(
    async_database_url, session_factory, monkeypatch
):
    """Two concurrent first-contact deliveries with DISTINCT message_ids for the same
    (DID, sender) pair must result in exactly ONE chat session — not two.

    Before Fix #1 both deliveries passed Gate 0 simultaneously (no row to lock) and each
    created a chat. The advisory lock in lock_sms_autocreate serializes them so the second
    delivery blocks, then Gate 0 sees the committed chat and declines.

    The test uses two independent NullPool sessions (separate DB connections) so the
    advisory lock contention is real; asyncio.gather drives them concurrently in one loop.
    """
    import asyncio

    from usan_api import telnyx_messaging
    from usan_api.compat import inbound_autocreate as _mod
    from usan_api.schemas.inbound_sms import InboundSms
    from usan_api.settings import get_settings
    from usan_api.tenant_context import set_tenant_context

    # Set minimum required env vars so get_settings() validates, then model_copy the extras.
    monkeypatch.setenv("DATABASE_URL", async_database_url.replace("+asyncpg", ""))
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "a" * 32)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit:7880")
    monkeypatch.setenv("JWT_SIGNING_KEY", "s" * 32)
    monkeypatch.setenv("OPERATOR_API_KEY", "o" * 32)
    monkeypatch.setenv("TELNYX_INBOUND_SMS_AUTOCREATE_ENABLED", "true")
    monkeypatch.setenv("TELNYX_MESSAGING_ENABLED", "true")
    monkeypatch.setenv("TELNYX_MESSAGING_API_KEY", "test-key")
    monkeypatch.setenv("TELNYX_MESSAGING_PROFILE_ID", "test-profile")
    monkeypatch.setenv("TELNYX_FROM_NUMBER", _OUR)
    monkeypatch.setenv("GCP_PROJECT", "proj")
    get_settings.cache_clear()

    # Patch generate_agent_reply and send_sms on the module under test.
    async def _fake_reply(db, settings, session):
        return "reply"

    async def _fake_send(settings, *, to_number, body):
        return "tx-out"

    monkeypatch.setattr(_mod, "generate_agent_reply", _fake_reply)
    monkeypatch.setattr(telnyx_messaging, "send_sms", _fake_send)

    # Seed the bound number + live agent via session_factory (commits).
    await _seed_bound_number(session_factory)

    # Resolve the default org id so both sessions can set tenant context.
    async with session_factory() as seed_db:
        org_id = (await seed_db.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()

    settings = get_settings()

    # Two independent engines/sessions (separate connections) for real lock contention.
    engine_a = create_async_engine(async_database_url, poolclass=NullPool)
    engine_b = create_async_engine(async_database_url, poolclass=NullPool)
    factory_a = async_sessionmaker(engine_a, expire_on_commit=False)
    factory_b = async_sessionmaker(engine_b, expire_on_commit=False)

    inbound_m1 = InboundSms(
        message_id="concurrent-m1",
        from_number=_SENDER,
        to_number=_OUR,
        text="hello",
        event_type="message.received",
    )
    inbound_m2 = InboundSms(
        message_id="concurrent-m2",
        from_number=_SENDER,
        to_number=_OUR,
        text="world",
        event_type="message.received",
    )

    async def _run(factory, inbound):
        async with factory() as db:
            await set_tenant_context(db, org_id)
            return await _mod.handle_inbound_autocreate(db, settings, inbound)

    try:
        results = await asyncio.gather(
            _run(factory_a, inbound_m1),
            _run(factory_b, inbound_m2),
        )
    finally:
        await engine_a.dispose()
        await engine_b.dispose()

    get_settings.cache_clear()

    # Exactly one delivery should have created the chat; the other declined at Gate 0.
    assert sorted(results) == [False, True], (
        f"Expected one True (created) and one False (Gate 0 decline), got {results}"
    )
    assert await _session_count(session_factory) == 1, (
        "Advisory lock must prevent two concurrent first-contact deliveries from forking "
        "two chat sessions"
    )
