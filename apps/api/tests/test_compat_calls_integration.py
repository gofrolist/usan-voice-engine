"""Integration tests (T018): full create->get->list->stop->update lifecycle; number->Contact
lazy upsert (name/timezone defaults); synthesized-idempotency no-double-dial on retry; DNC
and quiet-hours each return an explicit 400 with a machine-readable reason."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import dialer, livekit_dispatch, quiet_hours


@pytest.fixture
def mock_dispatch(monkeypatch):
    agent = AsyncMock()
    scheduled: list = []
    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", agent)
    monkeypatch.setattr(
        dialer, "schedule_dial", lambda call_id, settings: scheduled.append(call_id)
    )
    agent.scheduled = scheduled
    return agent


@pytest.fixture
def allow_quiet_hours(monkeypatch):
    monkeypatch.setattr(quiet_hours, "next_allowed", lambda dt, tz, **k: dt)


def _create(compat_client, compat_headers, **overrides):
    body = {"from_number": "+15551230000", "to_number": "+15557654321"}
    body.update(overrides)
    return compat_client.post("/v2/create-phone-call", json=body, headers=compat_headers)


async def _contact_row(super_async_url, phone):
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT name, timezone, external_id FROM contacts WHERE phone_e164 = :p"),
                    {"p": phone},
                )
            ).one_or_none()
    finally:
        await engine.dispose()


async def _seed_dnc(super_async_url, phone):
    # Superuser bypasses RLS; organization_id defaults to the seeded usan org (the compat
    # key's org), so is_blocked under the compat session finds it.
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO dnc_list (phone_e164) VALUES (:p)"), {"p": phone})
    finally:
        await engine.dispose()


def test_lazy_contact_upsert_defaults(
    compat_client,
    compat_headers,
    published_default_agent,
    mock_dispatch,
    allow_quiet_hours,
    async_database_url,
):
    phone = "+15557654321"
    assert _create(compat_client, compat_headers).status_code == 201
    row = asyncio.run(_contact_row(async_database_url, phone))
    assert row is not None
    name, timezone, _external_id = row
    assert name == phone  # default name = the E.164 number
    assert timezone == "America/New_York"  # COMPAT_DEFAULT_TIMEZONE


def test_lazy_contact_uses_metadata_name_and_external_id(
    compat_client,
    compat_headers,
    published_default_agent,
    mock_dispatch,
    allow_quiet_hours,
    async_database_url,
):
    phone = "+15550009999"
    r = _create(
        compat_client,
        compat_headers,
        to_number=phone,
        metadata={"name": "Bob Smith", "external_id": "crm-42"},
    )
    assert r.status_code == 201
    name, _tz, external_id = asyncio.run(_contact_row(async_database_url, phone))
    assert name == "Bob Smith"
    assert external_id == "crm-42"


def test_synthesized_idempotency_no_double_dial(
    compat_client, compat_headers, published_default_agent, mock_dispatch, allow_quiet_hours
):
    r1 = _create(compat_client, compat_headers)
    r2 = _create(compat_client, compat_headers)  # identical -> same synth key -> replay
    assert r1.status_code == 201
    assert r2.json()["call_id"] == r1.json()["call_id"]
    assert len(mock_dispatch.scheduled) == 1  # dialed exactly once


def test_dnc_blocked_returns_explicit_400(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours, async_database_url
):
    phone = "+15553334444"
    asyncio.run(_seed_dnc(async_database_url, phone))
    r = _create(compat_client, compat_headers, to_number=phone)
    assert r.status_code == 400
    assert r.json()["message"] == "blocked_dnc"
    assert len(mock_dispatch.scheduled) == 0  # never dialed


def test_quiet_hours_blocked_returns_explicit_400(
    compat_client, compat_headers, published_default_agent, mock_dispatch, monkeypatch
):
    monkeypatch.setattr(quiet_hours, "next_allowed", lambda dt, tz, **k: dt + timedelta(hours=2))
    r = _create(compat_client, compat_headers, to_number="+15552223333")
    assert r.status_code == 400
    assert r.json()["message"] == "blocked_quiet_hours"
    assert len(mock_dispatch.scheduled) == 0  # never dialed


def test_metadata_preserves_types_round_trip(
    compat_client, compat_headers, published_default_agent, mock_dispatch, allow_quiet_hours
):
    # Non-string metadata must survive create -> get with original JSON types (review #3).
    r = _create(
        compat_client,
        compat_headers,
        to_number="+15558887777",
        metadata={"score": 42, "vip": True, "tags": ["a", "b"]},
    )
    assert r.status_code == 201
    cid = r.json()["call_id"]
    got = compat_client.get(f"/v2/get-call/{cid}", headers=compat_headers).json()
    assert got["metadata"]["score"] == 42  # int, not "42"
    assert got["metadata"]["vip"] is True  # bool, not "true"
    assert got["metadata"]["tags"] == ["a", "b"]  # nested preserved


def test_idempotency_invariant_to_phone_format(
    compat_client, compat_headers, published_default_agent, mock_dispatch, allow_quiet_hours
):
    # A retry that reformats the number must hit the same call, never double-dial (review #1).
    r1 = _create(compat_client, compat_headers, to_number="+1 (555) 888-1212")
    r2 = _create(compat_client, compat_headers, to_number="+15558881212")
    assert r1.status_code == 201
    assert r2.json()["call_id"] == r1.json()["call_id"]
    assert len(mock_dispatch.scheduled) == 1  # dialed exactly once


def test_reserved_meta_var_key_rejected(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    # A CRM dynamic-var key colliding with the reserved metadata namespace is a 422 (review #3/#4).
    r = _create(compat_client, compat_headers, retell_llm_dynamic_variables={"__meta__": "x"})
    assert r.status_code == 422
