import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usan_api.schemas.admin import ContactCreate, ContactUpdate
from usan_api.schemas.admin_calls import AdminCreateCallRequest
from usan_api.schemas.dnc import AdminDNCResponse


def test_contact_create_rejects_bad_phone():
    with pytest.raises(ValidationError):
        ContactCreate(name="A", phone_e164="5551234", timezone="America/Chicago")


def test_contact_create_rejects_bad_timezone():
    with pytest.raises(ValidationError):
        ContactCreate(name="A", phone_e164="+15551230000", timezone="Mars/Phobos")


def test_contact_create_ok():
    c = ContactCreate(name="A", phone_e164="+15551230000", timezone="America/Chicago")
    assert c.metadata == {}
    assert c.external_id is None


def test_contact_update_forbids_unknown_field():
    with pytest.raises(ValidationError):
        ContactUpdate(agent_profile_id=str(uuid.uuid4()))  # not editable here


def test_admin_create_call_defaults():
    body = AdminCreateCallRequest(contact_id=uuid.uuid4())
    assert body.dynamic_vars == {}
    assert body.profile_override is None


def test_admin_create_call_rejects_nested_dynamic_vars():
    with pytest.raises(ValidationError):
        AdminCreateCallRequest(contact_id=uuid.uuid4(), dynamic_vars={"k": {"nested": 1}})


def test_admin_create_call_rejects_oversized_dynamic_vars():
    big = {str(i): "x" * 100 for i in range(100)}  # serializes to > 8192 bytes
    with pytest.raises(ValidationError):
        AdminCreateCallRequest(contact_id=uuid.uuid4(), dynamic_vars=big)


def test_admin_dnc_response_masks():
    class _E:
        phone_e164 = "+15551239999"
        reason = "x"
        added_at = datetime(2026, 6, 23, tzinfo=UTC)

    out = AdminDNCResponse.from_model(_E())
    assert out.masked_phone.endswith("9999")
    assert "+1555" not in out.masked_phone
    assert out.masked_phone != "+15551239999"
