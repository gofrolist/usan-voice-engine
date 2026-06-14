"""flag.created / callback.created enqueue inside the creators (Task C5).

Pins spec §2.1's integration rows for the tool-side creators: the repository
creators fan the event into the transactional outbox in the SAME flush-only
transaction (the router's existing ``db.commit()`` makes business row and
event durable together, zero call-site edits), and the flag payload is the
§6.4 deliberate reduction — NO ``elder_id``, NO ``category``, NO ``reason``
even though the flag row carries all three.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import OPERATOR_HEADERS, service_token
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import FollowUpFlag, WebhookDelivery
from usan_api.db.models import WebhookEndpoint as _Endpoint
from usan_api.repositories import callback_requests as callbacks_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import follow_up_flags as flags_repo

_TABLES = "webhook_deliveries, webhook_endpoints, follow_up_flags, callback_requests"


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _truncate(async_database_url):
    # Wraps the test (before AND after, test_webhook_enqueue_calls precedent):
    # leftover webhook_endpoints rows would change fan-out counts in unrelated
    # modules. Sync + asyncio.run so the sync full-stack `client` test below can
    # share the same isolation (conftest's admin_session pattern).
    async def _wipe() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f"TRUNCATE {_TABLES} CASCADE"))
        finally:
            await engine.dispose()

    asyncio.run(_wipe())
    yield
    asyncio.run(_wipe())


async def _seed_endpoint(factory, *, events: list[str]) -> uuid.UUID:
    async with factory() as db:
        ep = _Endpoint(url="https://hooks.example.com/sink", secret="a" * 64, events=events)
        db.add(ep)
        await db.commit()
        return ep.id


async def _seed_call_and_elder(factory) -> tuple[uuid.UUID, uuid.UUID]:
    # Unique phone per call: this module shares the long-lived test Postgres
    # with modules that never truncate elders/calls.
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    async with factory() as db:
        elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
        call = await calls_repo.create_call(
            db,
            elder_id=elder.id,
            direction=CallDirection.OUTBOUND,
            status=CallStatus.IN_PROGRESS,
        )
        await db.commit()
        return call.id, elder.id


async def _deliveries(db, event: str) -> list[WebhookDelivery]:
    result = await db.execute(
        select(WebhookDelivery).where(
            WebhookDelivery.event == event, WebhookDelivery.status == "pending"
        )
    )
    return list(result.scalars().all())


async def test_create_flag_enqueues_in_same_txn(session_factory):
    await _seed_endpoint(session_factory, events=["flag.created"])
    call_id, elder_id = await _seed_call_and_elder(session_factory)

    async with session_factory() as db:
        row = await flags_repo.create_follow_up_flag(
            db,
            call_id=call_id,
            elder_id=elder_id,
            severity="urgent",
            category="medical",
            reason="reported chest pain",
        )
        await db.commit()
        flag_id = row.id

    async with session_factory() as db:
        rows = await _deliveries(db, "flag.created")
    assert len(rows) == 1
    data = rows[0].payload["data"]
    # §6.4 re-pinned at the integration level: exactly these four keys — the
    # flag row carries elder_id/category/reason, the payload must not.
    assert set(data.keys()) == {"flag_id", "call_id", "severity", "created_at"}
    assert data["flag_id"] == flag_id
    assert data["call_id"] == str(call_id)
    assert data["severity"] == "urgent"
    assert str(elder_id) not in json.dumps(rows[0].payload)


async def test_create_callback_enqueues_in_same_txn(session_factory):
    await _seed_endpoint(session_factory, events=["callback.created"])
    call_id, elder_id = await _seed_call_and_elder(session_factory)

    async with session_factory() as db:
        row = await callbacks_repo.create_callback_request(
            db,
            call_id=call_id,
            elder_id=elder_id,
            requested_time_text="SENTINEL_TIME_TEXT tomorrow morning",
            requested_at=datetime(2026, 6, 11, 9, 0, tzinfo=UTC),
            notes="SENTINEL_NOTES please call after breakfast",
        )
        await db.commit()
        callback_id = row.id

    async with session_factory() as db:
        rows = await _deliveries(db, "callback.created")
    assert len(rows) == 1
    data = rows[0].payload["data"]
    assert set(data.keys()) == {"callback_id", "call_id", "elder_id", "requested_at", "created_at"}
    assert data["callback_id"] == callback_id
    assert data["call_id"] == str(call_id)
    assert data["elder_id"] == str(elder_id)
    assert data["requested_at"].startswith("2026-06-11T09:00")
    # §6.5: the free-text fields never reach the serialized payload.
    serialized = json.dumps(rows[0].payload)
    assert "SENTINEL_TIME_TEXT" not in serialized
    assert "SENTINEL_NOTES" not in serialized


async def test_flag_rollback_discards_both(session_factory):
    await _seed_endpoint(session_factory, events=["flag.created"])
    call_id, elder_id = await _seed_call_and_elder(session_factory)

    async with session_factory() as db:
        row = await flags_repo.create_follow_up_flag(
            db,
            call_id=call_id,
            elder_id=elder_id,
            severity="routine",
            category="other",
            reason=None,
        )
        assert row.id is not None  # creator flushed both flag and enqueue
        await db.rollback()

    async with session_factory() as db:
        flags = await db.execute(select(func.count()).select_from(FollowUpFlag))
        assert int(flags.scalar_one()) == 0
        deliveries = await db.execute(select(func.count()).select_from(WebhookDelivery))
        assert int(deliveries.scalar_one()) == 0


def _seed_endpoint_sync(async_database_url: str, *, events: list[str]) -> None:
    async def _run() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            await _seed_endpoint(factory, events=events)
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _fetch_flag_deliveries(async_database_url: str) -> list[dict]:
    async def _run() -> list[dict]:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                rows = await _deliveries(db, "flag.created")
                return [row.payload for row in rows]
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_tool_endpoint_commit_covers_enqueue(client, monkeypatch, async_database_url):
    # Full stack: the agent-facing tool route's existing db.commit() makes the
    # flag row AND the outbox row durable together — zero call-site edits.
    from unittest.mock import AsyncMock

    from usan_api import dialer, livekit_dispatch

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)

    r = client.post(
        "/v1/elders",
        json={
            "name": "Ada",
            "phone_e164": f"+1555{str(uuid.uuid4().int)[:7]}",
            "timezone": "UTC",
            "metadata": {},
        },
        headers=OPERATOR_HEADERS,
    )
    assert r.status_code == 201
    elder_id = r.json()["id"]
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": f"c5-{uuid.uuid4()}", "dynamic_vars": {}},
        headers=OPERATOR_HEADERS,
    )
    assert r.status_code == 202
    call_id = r.json()["id"]

    _seed_endpoint_sync(async_database_url, events=["flag.created"])

    r = client.post(
        "/v1/tools/flag_for_followup",
        json={
            "call_id": call_id,
            "severity": "urgent",
            "category": "medical",
            "reason": "reported chest pain",
        },
        headers={"Authorization": f"Bearer {service_token(call_id)}"},
    )
    assert r.status_code == 200

    # Committed and visible from a fresh engine/session — not merely flushed.
    payloads = _fetch_flag_deliveries(async_database_url)
    assert len(payloads) == 1
    assert payloads[0]["data"]["call_id"] == call_id
    assert set(payloads[0]["data"].keys()) == {"flag_id", "call_id", "severity", "created_at"}
