import uuid
from unittest.mock import AsyncMock

import pytest

from usan_api import livekit_dispatch


def _create_elder(client) -> str:
    r = client.post(
        "/v1/elders",
        json={"name": "Ada", "phone_e164": "+15551234567", "timezone": "UTC"},
    )
    assert r.status_code == 201
    return r.json()["id"]


@pytest.fixture
def mock_dispatch(monkeypatch) -> AsyncMock:
    dispatch = AsyncMock()
    monkeypatch.setattr(livekit_dispatch, "dispatch_outbound_call", dispatch)
    return dispatch


def test_enqueue_call_dispatches_and_returns_202(client, mock_dispatch):
    elder_id = _create_elder(client)
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "k1", "dynamic_vars": {}},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["direction"] == "outbound"
    assert body["status"] == "dialing"
    mock_dispatch.assert_awaited_once()


def test_enqueue_call_idempotent_replay_returns_200(client, mock_dispatch):
    elder_id = _create_elder(client)
    r1 = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "dup", "dynamic_vars": {}},
    )
    r2 = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "dup", "dynamic_vars": {}},
    )
    assert r1.status_code == 202
    assert r2.status_code == 200
    assert r2.json()["id"] == r1.json()["id"]
    mock_dispatch.assert_awaited_once()


def test_enqueue_call_conflicting_idempotency_returns_409(client, mock_dispatch):
    elder_id = _create_elder(client)
    first = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "x", "dynamic_vars": {"a": 1}},
    )
    assert first.status_code == 202
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "x", "dynamic_vars": {"a": 2}},
    )
    assert r.status_code == 409


def test_enqueue_call_dnc_blocked(client, mock_dispatch):
    elder_id = _create_elder(client)
    assert (
        client.post("/v1/dnc", json={"phone_e164": "+15551234567", "reason": "test"}).status_code
        == 201
    )
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "d1", "dynamic_vars": {}},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "dnc_blocked"
    mock_dispatch.assert_not_awaited()


def test_enqueue_call_unknown_elder_returns_404(client, mock_dispatch):
    r = client.post(
        "/v1/calls",
        json={
            "elder_id": str(uuid.uuid4()),
            "idempotency_key": "z",
            "dynamic_vars": {},
        },
    )
    assert r.status_code == 404


def test_get_call_returns_status(client, mock_dispatch):
    elder_id = _create_elder(client)
    created = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "g1", "dynamic_vars": {}},
    )
    call_id = created.json()["id"]
    r = client.get(f"/v1/calls/{call_id}")
    assert r.status_code == 200
    assert r.json()["id"] == call_id


def test_enqueue_call_dispatch_config_error_returns_503(client, monkeypatch):
    async def _raise(*args, **kwargs):
        raise livekit_dispatch.OutboundDispatchError(
            "not configured: set LIVEKIT_SIP_OUTBOUND_TRUNK_ID"
        )

    monkeypatch.setattr(livekit_dispatch, "dispatch_outbound_call", _raise)
    elder_id = _create_elder(client)
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "err503", "dynamic_vars": {}},
    )
    assert r.status_code == 503
    # the generic detail must not leak internal config var names
    assert "LIVEKIT_SIP_OUTBOUND_TRUNK_ID" not in r.json()["detail"]
    # the call row was persisted as FAILED; an idempotent replay surfaces it
    replay = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "err503", "dynamic_vars": {}},
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "failed"


def test_enqueue_call_unexpected_dispatch_error_returns_502(client, monkeypatch):
    async def _raise(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(livekit_dispatch, "dispatch_outbound_call", _raise)
    elder_id = _create_elder(client)
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": "err502", "dynamic_vars": {}},
    )
    assert r.status_code == 502


def test_get_unknown_call_returns_404(client):
    r = client.get(f"/v1/calls/{uuid.uuid4()}")
    assert r.status_code == 404
