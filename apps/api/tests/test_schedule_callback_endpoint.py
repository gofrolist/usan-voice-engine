import time
import uuid

import jwt
import pytest

from usan_api import livekit_dispatch

_OP = {"Authorization": "Bearer " + "o" * 32}


def _counter_value(counter, **labels) -> float:
    """Read a Counter's cumulative value via the public collect() API.

    Avoids the private `._value.get()` internal. The `_total` sample carries the
    cumulative count; `labels` filters labeled counters (empty for unlabeled ones).
    """
    for metric in counter.collect():
        for sample in metric.samples:
            if sample.name.endswith("_total") and sample.labels == labels:
                return sample.value
    return 0.0


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


def _service_token(call_id: str, secret: str = "s" * 32) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )


def _auth(call_id: str) -> dict:
    return {"Authorization": f"Bearer {_service_token(call_id)}"}


def _create_elder(client) -> str:
    r = client.post(
        "/v1/elders",
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


def _enqueue(client, elder_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": f"cb-{uuid.uuid4()}", "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 202
    return r.json()["id"]


def test_schedule_callback_ok(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
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
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": call_id, "requested_time_text": "sometime soon"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert isinstance(r.json()["id"], int)


def test_schedule_callback_requires_token(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": call_id, "requested_time_text": "soon"},
    )
    assert r.status_code == 401


def test_schedule_callback_mismatch_403(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
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


def test_schedule_callback_empty_time_text_422(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": call_id, "requested_time_text": ""},
        headers=_auth(call_id),
    )
    assert r.status_code == 422


def test_schedule_callback_increments_metric(client, mock_dispatch):
    from usan_api.observability.custom_metrics import CALLBACK_REQUESTS_TOTAL

    before = _counter_value(CALLBACK_REQUESTS_TOTAL)
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": call_id, "requested_time_text": "soon"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert _counter_value(CALLBACK_REQUESTS_TOTAL) == before + 1
