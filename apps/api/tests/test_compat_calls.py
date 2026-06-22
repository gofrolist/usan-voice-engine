"""Contract tests (T017) for the RetellAI-compatible call endpoints: paths, field names,
status codes, and the {status,message} error envelope. Shapes flagged PENDING-FREEZE are
pinned against the captured CRM oracle before this contract freezes (tasks.md gate)."""

from __future__ import annotations

import re
from unittest.mock import AsyncMock

import pytest

from usan_api import dialer, livekit_dispatch, quiet_hours

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture
def mock_dispatch(monkeypatch):
    agent = AsyncMock()
    scheduled: list = []
    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", agent)
    monkeypatch.setattr(
        dialer, "schedule_dial", lambda call_id, settings: scheduled.append(call_id)
    )
    agent.scheduled = scheduled
    return agent


@pytest.fixture
def allow_quiet_hours(monkeypatch):
    # A freshly-upserted contact defaults to America/New_York; neutralize the create-time
    # quiet-hours gate so the happy path is wall-clock-deterministic.
    monkeypatch.setattr(quiet_hours, "next_allowed", lambda dt, tz, **k: dt)


def _create(compat_client, compat_headers, **overrides):
    body = {"from_number": "+15551230000", "to_number": "+15557654321"}
    body.update(overrides)
    return compat_client.post("/v2/create-phone-call", json=body, headers=compat_headers)


def test_missing_key_returns_401_envelope(compat_client):
    r = compat_client.post(
        "/v2/create-phone-call", json={"from_number": "+15551230000", "to_number": "+15557654321"}
    )
    assert r.status_code == 401
    body = r.json()
    assert body["status"] == 401
    assert "message" in body
    assert "detail" not in body


def test_create_phone_call_returns_201_call_object(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    r = _create(
        compat_client,
        compat_headers,
        metadata={"crm_id": "abc"},
        retell_llm_dynamic_variables={"first_name": "Ada"},
    )
    assert r.status_code == 201, r.text
    call = r.json()
    assert _HEX32.match(call["call_id"])  # bare 32-char hex (RetellAI shape)
    assert call["call_type"] == "phone_call"
    assert call["direction"] == "outbound"
    assert call["call_status"] in ("registered", "ongoing")
    assert call["to_number"] == "+15557654321"
    assert call["from_number"] == "+15551230000"
    assert call["metadata"]["crm_id"] == "abc"  # CRM metadata round-trips
    assert call["retell_llm_dynamic_variables"]["first_name"] == "Ada"
    mock_dispatch.assert_awaited_once()


def test_get_call_returns_same_id(compat_client, compat_headers, mock_dispatch, allow_quiet_hours):
    created = _create(compat_client, compat_headers).json()
    r = compat_client.get(f"/v2/get-call/{created['call_id']}", headers=compat_headers)
    assert r.status_code == 200
    assert r.json()["call_id"] == created["call_id"]


def test_get_unknown_call_returns_404_envelope(compat_client, compat_headers):
    r = compat_client.get("/v2/get-call/" + "0" * 32, headers=compat_headers)
    assert r.status_code == 404
    body = r.json()
    assert body["status"] == 404
    assert "message" in body


def test_get_malformed_call_id_returns_422(compat_client, compat_headers):
    r = compat_client.get("/v2/get-call/not-a-valid-hex-id", headers=compat_headers)
    assert r.status_code == 422
    assert r.json()["status"] == 422


def test_list_calls_returns_envelope(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    _create(compat_client, compat_headers)
    r = compat_client.post(
        "/v3/list-calls", json={"limit": 10, "include_total": True}, headers=compat_headers
    )
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data["items"], list)
    assert len(data["items"]) >= 1
    assert _HEX32.match(data["items"][0]["call_id"])
    assert "has_more" in data
    assert data["total"] >= 1


def test_stop_call_returns_204(compat_client, compat_headers, mock_dispatch, allow_quiet_hours):
    created = _create(compat_client, compat_headers).json()
    r = compat_client.post(f"/v2/stop-call/{created['call_id']}", headers=compat_headers)
    assert r.status_code == 204


def test_update_call_echoes_metadata(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    created = _create(compat_client, compat_headers).json()
    r = compat_client.patch(
        f"/v2/update-call/{created['call_id']}",
        json={"metadata": {"stage": "qualified"}},
        headers=compat_headers,
    )
    assert r.status_code == 200
    assert r.json()["metadata"]["stage"] == "qualified"


def test_list_calls_rejects_skip_and_pagination_key_together(compat_client, compat_headers):
    # skip and pagination_key are mutually exclusive — sending both is a clean 422 in the
    # RetellAI envelope, never a silently double-paginated page (keyset WHERE + OFFSET).
    r = compat_client.post(
        "/v3/list-calls",
        json={"skip": 5, "pagination_key": "abc123"},
        headers=compat_headers,
    )
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == 422
    assert "invalid request" in body["message"]
