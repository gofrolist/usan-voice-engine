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
    compat_client, compat_headers, published_default_agent, mock_dispatch, allow_quiet_hours
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


def test_create_phone_call_unresolvable_timezone_fails_closed(
    compat_client, compat_headers, published_default_agent, mock_dispatch, monkeypatch
):
    """H-3: an unresolvable contact timezone must FAIL CLOSED (400 blocked_quiet_hours),
    not fall through to an immediate dispatch we cannot prove is inside TCPA hours."""

    def _raise(_dt, _tz, **_k):
        raise ValueError("unknown timezone")

    monkeypatch.setattr(quiet_hours, "next_allowed", _raise)
    r = _create(compat_client, compat_headers)
    assert r.status_code == 400, r.text
    assert r.json()["message"] == "blocked_quiet_hours"
    mock_dispatch.assert_not_awaited()  # no SIP call placed


def test_get_call_returns_same_id(
    compat_client, compat_headers, published_default_agent, mock_dispatch, allow_quiet_hours
):
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
    compat_client, compat_headers, published_default_agent, mock_dispatch, allow_quiet_hours
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


def test_stop_call_returns_204(
    compat_client, compat_headers, published_default_agent, mock_dispatch, allow_quiet_hours
):
    created = _create(compat_client, compat_headers).json()
    r = compat_client.post(f"/v2/stop-call/{created['call_id']}", headers=compat_headers)
    assert r.status_code == 204


def test_stop_call_force_hangs_up_live_room(
    compat_client,
    compat_headers,
    published_default_agent,
    mock_dispatch,
    allow_quiet_hours,
    monkeypatch,
):
    """stop-call on a non-terminal call with a livekit_room must invoke force_hangup."""
    import uuid

    from usan_api.compat.routers import calls as calls_router
    from usan_api.db.base import CallDirection, CallStatus
    from usan_api.db.models import Call

    hung_up: list = []

    async def _fake_force_hangup(room, settings):
        hung_up.append(room)

    monkeypatch.setattr(calls_router.livekit_dispatch, "force_hangup", _fake_force_hangup)

    # Monkeypatch _load_call to return a non-terminal call with a livekit_room set,
    # and monkeypatch set_status to a no-op so we don't need an actual DB row.
    fake_call = Call(
        id=uuid.uuid4(),
        direction=CallDirection.OUTBOUND,
        status=CallStatus.DIALING,
        livekit_room="test-room-xyz",
        dynamic_vars={},
    )

    async def _fake_load_call(db, call_id):
        return fake_call

    monkeypatch.setattr(calls_router, "_load_call", _fake_load_call)

    calls_set: list = []

    async def _fake_set_status(db, call_id, new_status):
        calls_set.append(new_status)

    monkeypatch.setattr(calls_router.calls_repo, "set_status", _fake_set_status)

    created = _create(compat_client, compat_headers).json()
    r = compat_client.post(f"/v2/stop-call/{created['call_id']}", headers=compat_headers)
    assert r.status_code == 204
    assert "test-room-xyz" in hung_up


def test_stop_call_terminal_call_does_not_force_hangup(
    compat_client,
    compat_headers,
    published_default_agent,
    mock_dispatch,
    allow_quiet_hours,
    monkeypatch,
):
    """stop-call on an already-terminal call must NOT invoke force_hangup."""
    from usan_api.compat.routers import calls as calls_router

    hung_up: list = []

    async def _fake_force_hangup(room, settings):
        hung_up.append(room)

    monkeypatch.setattr(calls_router.livekit_dispatch, "force_hangup", _fake_force_hangup)

    # First stop the call to make it terminal (CANCELLED).
    created = _create(compat_client, compat_headers).json()
    call_id_str = created["call_id"]
    compat_client.post(f"/v2/stop-call/{call_id_str}", headers=compat_headers)
    hung_up.clear()  # clear from first stop

    # Second stop — call is already terminal, force_hangup must NOT be called.
    r = compat_client.post(f"/v2/stop-call/{call_id_str}", headers=compat_headers)
    assert r.status_code == 204
    assert hung_up == []


def test_update_call_echoes_metadata(
    compat_client, compat_headers, published_default_agent, mock_dispatch, allow_quiet_hours
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
