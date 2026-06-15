"""T011 (US1): contract test for POST /v1/tools/raise_crisis.

Per-category resource script, idempotency per (call_id, category), and the standard
tool-auth contract (token required / call_id match / known call / valid enum).
"""

import time
import uuid

import jwt
import pytest

from usan_api import livekit_dispatch

_OP = {"Authorization": "Bearer " + "o" * 32}


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


def _create_elder(client, *, metadata: dict | None = None) -> str:
    r = client.post(
        "/v1/elders",
        json={
            "name": "Ada",
            "phone_e164": f"+1555{str(uuid.uuid4().int)[:7]}",
            "timezone": "UTC",
            "metadata": metadata or {},
        },
        headers=_OP,
    )
    assert r.status_code == 201
    return r.json()["id"]


def _enqueue(client, elder_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={
            "elder_id": elder_id,
            "idempotency_key": f"crisis-{uuid.uuid4()}",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    assert r.status_code == 202
    return r.json()["id"]


_EXPECTED = {
    "suicidal": ("988 Suicide & Crisis Lifeline", "988"),
    "medical": ("911 Emergency Services", "911"),
    "abuse": ("Adult Protective Services (Eldercare Locator)", "1-800-677-1116"),
    "confusion": ("911 Emergency Services", "911"),
    "overdose": ("Poison Control", "1-800-222-1222"),
}


@pytest.mark.parametrize("category", list(_EXPECTED))
def test_raise_crisis_returns_resource_per_category(client, mock_dispatch, category):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": category, "detection_source": "llm"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    label, number = _EXPECTED[category]
    assert body["resource_label"] == label
    assert body["resource_number"] == number
    assert body["spoken_script"]
    assert isinstance(body["flag_id"], int)


def test_raise_crisis_idempotent_per_call_and_category(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    first = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "suicidal", "detection_source": "llm"},
        headers=_auth(call_id),
    )
    second = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "suicidal", "detection_source": "safety_net"},
        headers=_auth(call_id),
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    # Same (call_id, category) -> the same flag row (idempotent upsert), not a duplicate.
    assert first.json()["flag_id"] == second.json()["flag_id"]


def test_raise_crisis_distinct_categories_are_distinct_flags(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    a = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "suicidal", "detection_source": "llm"},
        headers=_auth(call_id),
    )
    b = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "medical", "detection_source": "llm"},
        headers=_auth(call_id),
    )
    assert a.json()["flag_id"] != b.json()["flag_id"]


def test_raise_crisis_requires_token(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "medical", "detection_source": "llm"},
    )
    assert r.status_code == 401


def test_raise_crisis_call_id_mismatch_403(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "medical", "detection_source": "llm"},
        headers=_auth(str(uuid.uuid4())),
    )
    assert r.status_code == 403


def test_raise_crisis_unknown_call_404(client, mock_dispatch):
    cid = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": cid, "category": "medical", "detection_source": "llm"},
        headers=_auth(cid),
    )
    assert r.status_code == 404


def test_raise_crisis_bad_category_422(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "panic", "detection_source": "llm"},
        headers=_auth(call_id),
    )
    assert r.status_code == 422


def test_raise_crisis_bad_detection_source_422(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/raise_crisis",
        json={"call_id": call_id, "category": "medical", "detection_source": "guess"},
        headers=_auth(call_id),
    )
    assert r.status_code == 422
