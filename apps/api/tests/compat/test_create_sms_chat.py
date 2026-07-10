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


def test_to_number_normalized_to_e164_at_create(
    compat_client, compat_headers, web_agent_id, sms_messaging_enabled, mock_send_sms
):
    """to_number with punctuation/spaces is normalized to E.164 before both the Telnyx send
    and the stored row, so the inbound matcher (find_open_sms_chat) can match by to_e164."""
    # "1 (555) 123-4567" -> 11 digits starting with 1 -> "+15551234567"
    non_e164 = "1 (555) 123-4567"
    expected_e164 = "+15551234567"
    r = _create_sms(
        compat_client, compat_headers, override_agent_id=web_agent_id, to_number=non_e164
    )
    assert r.status_code == 200, r.text
    assert len(mock_send_sms) == 1
    assert mock_send_sms[0]["to_number"] == expected_e164


def test_greeting_renders_real_clock_not_blank(
    compat_client, compat_headers, sms_messaging_enabled, mock_send_sms, make_published_agent
):
    # A greeting using {{current_time}} must render a real clock value in the SMS, not a
    # blank — regression for the /review #142 timezone="" finding. The compat voice path
    # passes settings.compat_default_timezone to build_vars; the SMS path now matches.
    agent_id = make_published_agent("The time is {{current_time}} now")
    r = _create_sms(compat_client, compat_headers, override_agent_id=agent_id)
    assert r.status_code == 200, r.text
    body = mock_send_sms[0]["body"]
    assert body.startswith("The time is "), body  # greeting propagated
    assert "{{" not in body  # the token was substituted
    assert any(ch.isdigit() for ch in body), body  # a real clock value, not blank


def _stored_from_number(dsn: str, session_id: str) -> str:
    async def _run() -> str:
        engine = create_async_engine(dsn, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return (
                    await conn.execute(
                        sa_text("SELECT from_number FROM chat_sessions WHERE id = :i"),
                        {"i": session_id},
                    )
                ).scalar_one()
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def test_from_number_normalized_to_e164_at_create(
    compat_client, compat_headers, web_agent_id, mock_send_sms, async_database_url
):
    """from_number is normalized to E.164 in the stored row, so the inbound matcher
    (find_open_sms_chat, comparing from_number == to_e164(inbound.to_number)) matches even
    when TELNYX_FROM_NUMBER is configured non-strict-E.164. Symmetric with the to_number fix.
    Asserted on the stored row (send_sms uses settings.telnyx_from_number, not body)."""
    from starlette.routing import Mount

    from usan_api.compat import ids
    from usan_api.settings import Settings

    national = "1 (555) 000-0000"  # 11 digits starting with 1 -> "+15550000000"
    compat_app = next(r.app for r in compat_client.app.routes if isinstance(r, Mount))
    base = get_settings()

    def _override() -> Settings:
        return base.model_copy(
            update={
                "telnyx_messaging_enabled": True,
                "telnyx_messaging_api_key": SecretStr("test-key"),
                "telnyx_messaging_profile_id": "test-profile",
                "telnyx_from_number": national,
            }
        )

    compat_app.dependency_overrides[get_settings] = _override
    try:
        r = _create_sms(
            compat_client, compat_headers, from_number=national, override_agent_id=web_agent_id
        )
        assert r.status_code == 200, r.text
        chat_id = r.json()["chat_id"]
    finally:
        compat_app.dependency_overrides.pop(get_settings, None)

    assert (
        _stored_from_number(async_database_url, str(ids.decode_chat_id(chat_id))) == "+15550000000"
    )
