"""Phone-number compat schema unit tests: StrictBool, weight bounds, SSRF URL, serializer."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usan_api.compat.schemas.phone_numbers import (
    AgentWeight,
    ImportPhoneNumberRequest,
    serialize_phone_number,
)
from usan_api.db.models import PhoneNumber


def test_ignore_e164_validation_rejects_string_literal() -> None:
    # StrictBool: the JSON string "true" is NOT coerced — oracle requires a bool literal.
    with pytest.raises(ValidationError):
        ImportPhoneNumberRequest(
            phone_number="+15550000001", termination_uri="x", ignore_e164_validation="true"
        )


def test_agent_weight_bounds() -> None:
    AgentWeight(agent_id="agent_" + uuid.uuid4().hex, weight=1.0)  # ok
    with pytest.raises(ValidationError):
        AgentWeight(agent_id="agent_x", weight=0)  # gt=0
    with pytest.raises(ValidationError):
        AgentWeight(agent_id="", weight=0.5)  # min_length=1


def test_inbound_webhook_url_ssrf_rejected() -> None:
    with pytest.raises(ValidationError):
        ImportPhoneNumberRequest(
            phone_number="+15550000001",
            termination_uri="x",
            inbound_webhook_url="http://169.254.169.254/latest",  # not https + IP literal
        )


def test_serializer_omits_password_and_builds_trunk_config() -> None:
    pn = PhoneNumber(
        id=uuid.uuid4(),
        phone_e164="+15550000001",
        phone_number_type="custom",
        termination_uri="sip.example.com",
        sip_auth_username="user",
        sip_auth_password="secret",
        transport="TCP",
        updated_at=datetime.now(UTC),
    )
    out = serialize_phone_number(pn).model_dump(exclude_none=True)
    assert out["phone_number"] == "+15550000001"
    assert out["phone_number_type"] == "custom"
    assert isinstance(out["last_modification_timestamp"], int)
    assert out["sip_outbound_trunk_config"] == {
        "termination_uri": "sip.example.com",
        "auth_username": "user",
        "transport": "TCP",
    }
    # password never surfaces, anywhere
    assert "auth_password" not in out["sip_outbound_trunk_config"]
    assert "secret" not in str(out)
