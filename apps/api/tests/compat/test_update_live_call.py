"""Frozen: PATCH /v2/update-live-call/{call_id} — {success} ack + mid-call var injection.

SDK note: retell-sdk 5.53.0 has no named response model for update-live-call
(only call_update_params.py exists, no CallUpdateResponse in retell.types.__init__).
We therefore assert the literal {"success": True} only — no assert_sdk_roundtrip.

Room-push test: a freshly-created call has no livekit_room, so send_dynamic_vars is
skipped on the basic ack path. The room-push path is tested by monkeypatching _load_call
to return a stub Call with livekit_room set.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from tests.compat.conftest import _published_agent_id, create_call

pytestmark = pytest.mark.frozen


def test_update_live_call_acks_success(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    """Basic ack path: no livekit_room on a freshly-created call, send_dynamic_vars skipped."""
    agent_id = _published_agent_id(compat_client, compat_headers)
    call_id = create_call(compat_client, compat_headers, override_agent_id=agent_id).json()[
        "call_id"
    ]

    resp = compat_client.patch(
        f"/v2/update-live-call/{call_id}",
        json={
            "fields_to_override": {"override_dynamic_variables": {"first_name": "Ada"}},
            "call_control": {"trigger_response": True, "additional_context": "be brief"},
        },
        headers=compat_headers,
    )
    assert resp.status_code == 200, resp.text
    # No named SDK response model — assert literal shape only.
    assert resp.json() == {"success": True}


def test_update_live_call_pushes_vars_when_room_set(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours, monkeypatch
):
    """Room-push path: monkeypatch _load_call to return a stub Call with livekit_room set,
    and capture send_dynamic_vars args to assert the push was awaited correctly."""
    from usan_api.compat.routers import calls as calls_router

    sent: dict = {}

    async def _fake_send(room: str, variables: dict, settings) -> None:  # noqa: ANN001
        sent["room"] = room
        sent["vars"] = variables

    monkeypatch.setattr(calls_router.livekit_dispatch, "send_dynamic_vars", _fake_send)

    # Build a minimal stub Call that _load_call would return.
    stub_call = MagicMock()
    stub_call.id = uuid.uuid4()
    stub_call.livekit_room = "room-xyz"
    stub_call.dynamic_vars = None  # unpack_dynamic_vars handles None
    stub_call.archived_at = None

    original_load = calls_router._load_call

    async def _patched_load(db, call_id: str):  # noqa: ANN001
        if call_id == "call_testroom":
            return stub_call
        return await original_load(db, call_id)

    monkeypatch.setattr(calls_router, "_load_call", _patched_load)

    resp = compat_client.patch(
        "/v2/update-live-call/call_testroom",
        json={"fields_to_override": {"override_dynamic_variables": {"first_name": "Ada"}}},
        headers=compat_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True}
    assert sent.get("room") == "room-xyz"
    assert sent.get("vars") == {"first_name": "Ada"}


def test_update_live_call_no_vars_acks_without_push(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours, monkeypatch
):
    """call_control-only body (no override_dynamic_variables): ack true, no push."""
    from usan_api.compat.routers import calls as calls_router

    push_called = []

    async def _fake_send(room: str, variables: dict, settings) -> None:  # noqa: ANN001
        push_called.append(True)

    monkeypatch.setattr(calls_router.livekit_dispatch, "send_dynamic_vars", _fake_send)

    agent_id = _published_agent_id(compat_client, compat_headers)
    call_id = create_call(compat_client, compat_headers, override_agent_id=agent_id).json()[
        "call_id"
    ]

    resp = compat_client.patch(
        f"/v2/update-live-call/{call_id}",
        json={"call_control": {"trigger_response": False, "additional_context": "silent"}},
        headers=compat_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"success": True}
    assert push_called == []  # no vars → no push


def test_update_live_call_unknown_is_404(compat_client, compat_headers):
    assert (
        compat_client.patch(
            "/v2/update-live-call/call_nope", json={}, headers=compat_headers
        ).status_code
        == 404
    )
