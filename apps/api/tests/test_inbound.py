import time
import uuid

import jwt

from tests.conftest import OPERATOR_HEADERS as _OP
from tests.conftest import service_token as _service_token

SECRET = "s" * 32
# Operator bearer token for the management plane (matches conftest's OPERATOR_API_KEY).


def _worker_token(secret: str = SECRET) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "iat": now, "exp": now + 300}, secret, algorithm="HS256"
    )


def _worker_auth() -> dict:
    return {"Authorization": f"Bearer {_worker_token()}"}


def _create_contact(client, phone: str) -> str:
    r = client.post(
        "/v1/contacts",
        json={"name": "Ada", "phone_e164": phone, "timezone": "UTC", "metadata": {}},
        headers=_OP,
    )
    assert r.status_code == 201
    return r.json()["id"]


def _phone() -> str:
    return f"+1555{str(uuid.uuid4().int)[:7]}"


def test_inbound_known_contact_creates_call_and_returns_vars(client):
    phone = _phone()
    contact_id = _create_contact(client, phone)
    r = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": phone, "livekit_room": "usan-inbound-1"},
        headers=_worker_auth(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["contact_known"] is True
    assert data["dynamic_vars"]["contact_name"] == "Ada"
    call = client.get(f"/v1/calls/{data['call_id']}", headers=_OP).json()
    assert call["direction"] == "inbound"
    assert call["status"] == "in_progress"
    assert call["contact_id"] == contact_id


def test_inbound_matches_contact_when_caller_id_lacks_country_code(client):
    # Telnyx delivers the caller-ID as a bare US national number (no +1), but the
    # contact is stored E.164. The lookup must normalize and still match by name.
    national = "555" + str(uuid.uuid4().int)[:7]  # 10-digit national number
    contact_id = _create_contact(client, "+1" + national)
    r = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": national, "livekit_room": "usan-inbound-natl"},
        headers=_worker_auth(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["contact_known"] is True
    assert data["dynamic_vars"]["contact_name"] == "Ada"
    call = client.get(f"/v1/calls/{data['call_id']}", headers=_OP).json()
    assert call["contact_id"] == contact_id


def test_inbound_unknown_caller_creates_call_without_contact(client):
    r = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": "+19998887777", "livekit_room": "usan-inbound-2"},
        headers=_worker_auth(),
    )
    assert r.status_code == 200
    data = r.json()
    assert data["contact_known"] is False
    assert data["dynamic_vars"] == {}
    call = client.get(f"/v1/calls/{data['call_id']}", headers=_OP).json()
    assert call["direction"] == "inbound"
    assert call["contact_id"] is None


def test_inbound_no_phone_is_unknown(client):
    r = client.post(
        "/v1/calls/inbound",
        json={"livekit_room": "usan-inbound-3"},
        headers=_worker_auth(),
    )
    assert r.status_code == 200
    assert r.json()["contact_known"] is False


def test_inbound_requires_worker_token(bare_client):
    r = bare_client.post(
        "/v1/calls/inbound",
        json={"phone_e164": "+19998887777", "livekit_room": "usan-inbound-4"},
    )
    assert r.status_code == 401


def test_inbound_known_contact_returns_resolved_vars_and_timezone(client):
    phone = _phone()
    # Create an contact with a med schedule via metadata so today_meds populates.
    r = client.post(
        "/v1/contacts",
        json={
            "name": "Margaret Doe",
            "phone_e164": phone,
            "timezone": "US/Eastern",
            "metadata": {"medication_schedule": [{"name": "Lisinopril"}]},
        },
        headers=_OP,
    )
    assert r.status_code == 201
    resp = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": phone, "livekit_room": "usan-inbound-rv"},
        headers=_worker_auth(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["timezone"] == "US/Eastern"
    rv = data["resolved_vars"]
    assert rv["first_name"] == "Margaret"
    assert rv["contact_name"] == "Margaret Doe"
    assert rv["call_direction"] == "inbound"
    assert rv["today_meds"] == "Lisinopril"
    # current_time/current_date are agent-side — never in resolved_vars.
    assert "current_time" not in rv
    # The persisted idempotency-payload dynamic_vars is untouched by built-ins.
    assert "first_name" not in data["dynamic_vars"]


def test_inbound_unknown_caller_returns_empty_resolved_vars(client):
    resp = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": "+19990001111", "livekit_room": "usan-inbound-rv2"},
        headers=_worker_auth(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["resolved_vars"]["call_direction"] == "inbound"
    assert data["resolved_vars"]["first_name"] == ""
    assert data["timezone"] == ""


def test_inbound_surfaces_last_check_in_and_call_id_works_with_tools(client):
    phone = _phone()
    _create_contact(client, phone)
    first = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": phone, "livekit_room": "usan-inbound-5a"},
        headers=_worker_auth(),
    ).json()
    call_id = first["call_id"]
    # The inbound call_id works with a call-scoped tool token (proves JWT chaining).
    w = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": call_id, "mood": 4, "pain_level": 1, "notes": "a bit tired"},
        headers={"Authorization": f"Bearer {_service_token(call_id)}"},
    )
    assert w.status_code == 200
    # A later inbound call from the same contact surfaces the last check-in.
    second = client.post(
        "/v1/calls/inbound",
        json={"phone_e164": phone, "livekit_room": "usan-inbound-5b"},
        headers=_worker_auth(),
    ).json()
    assert "last_check_in" in second["dynamic_vars"]
    assert "mood 4/5" in second["dynamic_vars"]["last_check_in"]
