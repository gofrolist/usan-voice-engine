"""WS-E: POST /v1/tools/external — the client HTTP tool execution proxy.

Headline correctness gate: the outbound body is FLAT (no `args` wrapper) with {{var}} resolved
from the call's dynamic vars, and X-Caller-Secret is attached. Plus flag-gating (501), tool
resolution (404), and the egress allow-list (502). The client HTTP call is mocked via
httpx.MockTransport; SSRF DNS is stubbed so no real network/resolution happens.
"""

import json
import uuid

import httpx
import pytest

# Reuse the tool-plane helpers (service token, contact creation).
from tests.test_tools import _auth, _create_contact
from usan_api.routers import tools
from usan_api.schemas.agent_config import ExternalToolSpec

_OP = {"Authorization": "Bearer " + "o" * 32}
_ALLOWED = "client.example.com"


@pytest.fixture
def mock_dispatch(monkeypatch):
    """Neutralize LiveKit dispatch + the dialer so enqueuing a call places no real call
    (a local copy of the test_tools fixture, avoiding an imported-fixture F811)."""
    from unittest.mock import AsyncMock

    from usan_api import dialer, livekit_dispatch

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


@pytest.fixture
def enable_tools(monkeypatch):
    from usan_api.settings import get_settings

    monkeypatch.setenv("COMPAT_EXTERNAL_TOOLS_ENABLED", "true")
    monkeypatch.setenv("COMPAT_TOOL_ALLOWED_HOSTS", _ALLOWED)
    monkeypatch.setenv("COMPAT_TOOL_CALLER_SECRET", "sekret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _enqueue_vars(client, contact_id: str, dynamic_vars: dict) -> str:
    r = client.post(
        "/v1/calls",
        json={
            "contact_id": contact_id,
            "idempotency_key": f"ext-{uuid.uuid4()}",
            "dynamic_vars": dynamic_vars,
        },
        headers=_OP,
    )
    assert r.status_code == 202, r.text
    return r.json()["id"]


def _spec(url=f"https://{_ALLOWED}/functions/v1/schedule-callback"):
    return ExternalToolSpec(
        name="schedule_callback",
        description="Schedule a callback.",
        url=url,
        method="POST",
        parameters={"type": "object", "properties": {"phone": {"type": "string"}}},
    )


def _mock_http(monkeypatch, captured, *, status=200, payload=None, text=None):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["content"] = request.content
        captured["headers"] = dict(request.headers)
        if text is not None:
            return httpx.Response(status, text=text)
        return httpx.Response(status, json=payload if payload is not None else {"scheduled": True})

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient  # capture BEFORE patching to avoid recursion

    def fake(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(tools.httpx, "AsyncClient", fake)


def _stub_dns(monkeypatch):
    async def _ok(host):
        return ["203.0.113.10"]

    monkeypatch.setattr(tools.ssrf_guard, "resolve_public_or_raise", _ok)


# --- headline: flat body + {{var}} + X-Caller-Secret ------------------------


def test_flat_body_var_substitution_and_secret(client, mock_dispatch, enable_tools, monkeypatch):
    contact = _create_contact(client)
    call_id = _enqueue_vars(client, contact, {"phone": "+15551234567"})

    async def _resolve(db, call, name):
        return _spec()

    monkeypatch.setattr(tools, "_resolve_external_tool", _resolve)
    _stub_dns(monkeypatch)
    captured: dict = {}
    _mock_http(monkeypatch, captured, payload={"scheduled": True})

    r = client.post(
        "/v1/tools/external",
        json={
            "call_id": call_id,
            "name": "schedule_callback",
            "arguments": {"phone": "{{phone}}", "note": "call me"},
        },
        headers=_auth(call_id),
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"scheduled": True}

    sent = json.loads(captured["content"])
    # FLAT body: no `args` wrapper, and {{phone}} resolved from the call's dynamic vars.
    assert sent == {"phone": "+15551234567", "note": "call me"}
    assert "args" not in sent
    assert captured["headers"]["x-caller-secret"] == "sekret"


def test_non_json_response_wrapped_as_text(client, mock_dispatch, enable_tools, monkeypatch):
    contact = _create_contact(client)
    call_id = _enqueue_vars(client, contact, {})

    async def _resolve(db, call, name):
        return _spec()

    monkeypatch.setattr(tools, "_resolve_external_tool", _resolve)
    _stub_dns(monkeypatch)
    captured: dict = {}
    _mock_http(monkeypatch, captured, text="OK plain")
    r = client.post(
        "/v1/tools/external",
        json={"call_id": call_id, "name": "schedule_callback", "arguments": {}},
        headers=_auth(call_id),
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"text": "OK plain"}


# --- gating + failure modes -------------------------------------------------


def test_501_when_disabled(client, mock_dispatch):
    contact = _create_contact(client)
    call_id = _enqueue_vars(client, contact, {})
    r = client.post(
        "/v1/tools/external",
        json={"call_id": call_id, "name": "x", "arguments": {}},
        headers=_auth(call_id),
    )
    assert r.status_code == 501, r.text


def test_404_unknown_tool_uses_real_resolution(client, mock_dispatch, enable_tools):
    # No monkeypatch of _resolve_external_tool: the default config has no external tools.
    contact = _create_contact(client)
    call_id = _enqueue_vars(client, contact, {})
    r = client.post(
        "/v1/tools/external",
        json={"call_id": call_id, "name": "nope", "arguments": {}},
        headers=_auth(call_id),
    )
    assert r.status_code == 404, r.text


def test_502_on_disallowed_host(client, mock_dispatch, enable_tools, monkeypatch):
    contact = _create_contact(client)
    call_id = _enqueue_vars(client, contact, {})

    async def _resolve(db, call, name):
        return _spec(url="https://evil.example.net/fn")  # not in the allow-list

    monkeypatch.setattr(tools, "_resolve_external_tool", _resolve)
    r = client.post(
        "/v1/tools/external",
        json={"call_id": call_id, "name": "schedule_callback", "arguments": {}},
        headers=_auth(call_id),
    )
    assert r.status_code == 502, r.text


def test_502_on_upstream_error(client, mock_dispatch, enable_tools, monkeypatch):
    contact = _create_contact(client)
    call_id = _enqueue_vars(client, contact, {})

    async def _resolve(db, call, name):
        return _spec()

    monkeypatch.setattr(tools, "_resolve_external_tool", _resolve)
    _stub_dns(monkeypatch)
    captured: dict = {}
    _mock_http(monkeypatch, captured, status=500, payload={"err": "boom"})
    r = client.post(
        "/v1/tools/external",
        json={"call_id": call_id, "name": "schedule_callback", "arguments": {}},
        headers=_auth(call_id),
    )
    assert r.status_code == 502, r.text


# --- unit: substitution helpers ---------------------------------------------


def test_substitute_vars_resolves_and_blanks_unknown():
    vals = {"phone": "+15551234567"}
    out = tools._substitute_vars("dial {{phone}} now {{missing}}", vals)
    assert out == "dial +15551234567 now "


def test_substitute_deep_walks_nested_structures():
    vals = {"phone": "+1555", "name": "Ada"}
    out = tools._substitute_deep(
        {"a": "{{phone}}", "b": [{"c": "{{name}}"}], "n": 5, "flag": True}, vals
    )
    assert out == {"a": "+1555", "b": [{"c": "Ada"}], "n": 5, "flag": True}
