"""Behavioral tests for POST /create-sms-chat and its helpers (Phase 4b-1)."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.compat.chat_service import _sms_send_ready
from usan_api.compat.schemas.chats import CreateSmsChatRequest
from usan_api.settings import get_settings

_SMS_FROM = "+15550000000"
_SMS_TO = "+15551234567"


def test_request_requires_from_and_to() -> None:
    with pytest.raises(ValidationError):
        CreateSmsChatRequest(to_number="+15551234567")  # missing from_number
    with pytest.raises(ValidationError):
        CreateSmsChatRequest(from_number="+15550000000")  # missing to_number


def test_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CreateSmsChatRequest(from_number="+15550000000", to_number="+15551234567", bogus="x")


def test_request_accepts_optionals() -> None:
    req = CreateSmsChatRequest(
        from_number="+15550000000",
        to_number="+15551234567",
        override_agent_id="agent_deadbeef",
        metadata={"crm": 1},
        retell_llm_dynamic_variables={"name": "Pat"},
    )
    assert req.override_agent_id == "agent_deadbeef"


def _settings_with(**overrides):
    return get_settings().model_copy(update=overrides)


def test_sms_send_ready_truth_table() -> None:
    ready = _settings_with(
        telnyx_messaging_enabled=True,
        telnyx_messaging_api_key=SecretStr("k"),
        telnyx_messaging_profile_id="p",
        telnyx_from_number="+15550000000",
    )
    assert _sms_send_ready(ready) is True
    assert _sms_send_ready(ready.model_copy(update={"telnyx_messaging_enabled": False})) is False
    assert _sms_send_ready(ready.model_copy(update={"telnyx_messaging_api_key": None})) is False
    assert _sms_send_ready(ready.model_copy(update={"telnyx_messaging_profile_id": None})) is False
    assert _sms_send_ready(ready.model_copy(update={"telnyx_from_number": None})) is False


def _create_sms(client, headers, *, from_number=_SMS_FROM, to_number=_SMS_TO, **extra):
    return client.post(
        "/create-sms-chat",
        json={"from_number": from_number, "to_number": to_number, **extra},
        headers=headers,
    )


def test_create_sms_chat_requires_key(compat_client):
    r = compat_client.post(
        "/create-sms-chat", json={"from_number": _SMS_FROM, "to_number": _SMS_TO}
    )
    assert r.status_code == 401


def test_503_when_messaging_disabled(compat_client, compat_headers, web_agent_id):
    # default settings: telnyx_messaging_enabled is False -> 503 before any write
    r = _create_sms(compat_client, compat_headers, override_agent_id=web_agent_id)
    assert r.status_code == 503, r.text


def test_422_from_number_not_provisioned(
    compat_client, compat_headers, web_agent_id, sms_messaging_enabled, mock_send_sms
):
    r = _create_sms(
        compat_client, compat_headers, from_number="+19998887777", override_agent_id=web_agent_id
    )
    assert r.status_code == 422, r.text
    assert mock_send_sms == []  # never sent


def test_422_no_agent_bound(compat_client, compat_headers, sms_messaging_enabled, mock_send_sms):
    # provisioned sender, no override and no phone-number binding -> 422
    r = _create_sms(compat_client, compat_headers)
    assert r.status_code == 422, r.text
    assert mock_send_sms == []


def test_200_with_override_agent_id(
    compat_client, compat_headers, web_agent_id, sms_messaging_enabled, mock_send_sms
):
    r = _create_sms(
        compat_client,
        compat_headers,
        override_agent_id=web_agent_id,
        retell_llm_dynamic_variables={"name": "Pat"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["chat_status"] == "ongoing"
    assert body["chat_type"] == "sms_chat"
    assert body["chat_id"].startswith("chat_")
    assert len(mock_send_sms) == 1
    assert mock_send_sms[0]["to_number"] == _SMS_TO
    assert mock_send_sms[0]["body"]  # the greeting was sent


def _seed_phone_binding(dsn: str, from_number: str, agent_id: str) -> None:
    async def _run() -> None:
        engine = create_async_engine(dsn, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    sa_text(
                        "INSERT INTO phone_numbers (phone_e164, phone_number_type, "
                        "outbound_sms_agents) VALUES (:e164, 'custom', CAST(:agents AS JSONB))"
                    ),
                    {
                        "e164": from_number,
                        "agents": json.dumps([{"agent_id": agent_id, "weight": 1.0}]),
                    },
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_200_with_outbound_sms_binding(
    compat_client,
    compat_headers,
    web_agent_id,
    sms_messaging_enabled,
    mock_send_sms,
    async_database_url,
):
    _seed_phone_binding(async_database_url, _SMS_FROM, web_agent_id)
    r = _create_sms(compat_client, compat_headers)  # no override_agent_id -> use the binding
    assert r.status_code == 200, r.text
    assert r.json()["chat_type"] == "sms_chat"
    assert len(mock_send_sms) == 1


def test_502_on_send_failure_rolls_back(
    compat_client, compat_headers, web_agent_id, sms_messaging_enabled, monkeypatch
):
    from usan_api import telnyx_messaging

    async def _boom(settings, *, to_number, body):
        raise telnyx_messaging.TelnyxMessagingError("send failed")

    monkeypatch.setattr(telnyx_messaging, "send_sms", _boom)

    r = _create_sms(compat_client, compat_headers, override_agent_id=web_agent_id)
    assert r.status_code == 502, r.text

    # rollback proof: no chat row persisted (fresh truncated DB -> list-chats is empty)
    listed = compat_client.post("/v3/list-chats", json={}, headers=compat_headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"] == []


def test_completion_rejects_sms_chat(
    compat_client, compat_headers, web_agent_id, sms_messaging_enabled, mock_send_sms
):
    created = _create_sms(compat_client, compat_headers, override_agent_id=web_agent_id)
    assert created.status_code == 200, created.text
    chat_id = created.json()["chat_id"]

    r = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat_id, "content": "hi"},
        headers=compat_headers,
    )
    assert r.status_code == 422, r.text
