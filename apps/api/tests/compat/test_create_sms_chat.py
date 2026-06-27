"""Behavioral tests for POST /create-sms-chat and its helpers (Phase 4b-1)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from usan_api.compat.chat_service import _sms_send_ready
from usan_api.compat.schemas.chats import CreateSmsChatRequest
from usan_api.settings import get_settings


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
