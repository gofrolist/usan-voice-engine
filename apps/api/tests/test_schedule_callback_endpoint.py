import uuid

import pytest

from tests.conftest import OPERATOR_HEADERS as _OP
from tests.conftest import counter_value as _counter_value
from tests.conftest import service_token as _service_token
from usan_api import livekit_dispatch


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


def _auth(call_id: str) -> dict:
    return {"Authorization": f"Bearer {_service_token(call_id)}"}


def _create_contact(client) -> str:
    r = client.post(
        "/v1/contacts",
        json={
            "name": "Ada",
            "phone_e164": f"+1555{str(uuid.uuid4().int)[:7]}",
            "timezone": "UTC",
            "metadata": {},
        },
        headers=_OP,
    )
    assert r.status_code == 201
    return r.json()["id"]


def _enqueue(client, contact_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={
            "contact_id": contact_id,
            "idempotency_key": f"cb-{uuid.uuid4()}",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    assert r.status_code == 202
    return r.json()["id"]


def test_schedule_callback_ok(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/schedule_callback",
        json={
            "call_id": call_id,
            "requested_time_text": "tomorrow afternoon",
            "requested_at": "2026-06-10T15:00:00Z",
            "notes": "prefers afternoons",
        },
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert isinstance(r.json()["id"], int)


def test_schedule_callback_minimal_no_iso_time(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": call_id, "requested_time_text": "sometime soon"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert isinstance(r.json()["id"], int)


def test_schedule_callback_requires_token(bare_client):
    r = bare_client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": str(uuid.uuid4()), "requested_time_text": "soon"},
    )
    assert r.status_code == 401


def test_schedule_callback_mismatch_403(bare_client):
    call_id = str(uuid.uuid4())
    r = bare_client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": call_id, "requested_time_text": "soon"},
        headers=_auth(str(uuid.uuid4())),
    )
    assert r.status_code == 403


def test_schedule_callback_unknown_call_404(client, mock_dispatch):
    cid = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": cid, "requested_time_text": "soon"},
        headers=_auth(cid),
    )
    assert r.status_code == 404


def test_schedule_callback_empty_time_text_422(bare_client):
    call_id = str(uuid.uuid4())
    r = bare_client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": call_id, "requested_time_text": ""},
        headers=_auth(call_id),
    )
    assert r.status_code == 422


def test_schedule_callback_increments_metric(client, mock_dispatch):
    from usan_api.observability.custom_metrics import CALLBACK_REQUESTS_TOTAL

    before = _counter_value(CALLBACK_REQUESTS_TOTAL)
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": call_id, "requested_time_text": "soon"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert _counter_value(CALLBACK_REQUESTS_TOTAL) == before + 1
