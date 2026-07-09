"""Surface 2A: the inbound-call-router egress (compat/inbound_router.route_inbound).

We POST {event: "call_inbound", call_inbound: {from_number, to_number}} and parse the strict
{call_inbound: {override_agent_id, dynamic_variables}} reply. Every failure path (flag off, no
URL, non-2xx, redirect, bad JSON, missing wrapper, legacy flat shape, SSRF) returns None so the
caller degrades. HTTP is faked via httpx.MockTransport through the _build_client seam; DNS via the
ssrf_guard._resolve seam (both mirror the compat webhook-delivery tests).
"""

from __future__ import annotations

import json

import httpx
import pytest

from usan_api import ssrf_guard
from usan_api.compat import inbound_router
from usan_api.compat.inbound_router import InboundRouterResult
from usan_api.settings import Settings

_URL = "https://client.example.com/functions/v1/inbound-call-router"
_OK_BODY = {
    "call_inbound": {
        "override_agent_id": "agent_deadbeef",
        "dynamic_variables": {"first_name": "John", "trial_status": "active"},
    }
}


def _settings(**over) -> Settings:
    base = dict(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="a" * 32,
        LIVEKIT_URL="ws://livekit:7880",
        JWT_SIGNING_KEY="s" * 32,
        OPERATOR_API_KEY="o" * 16,
        COMPAT_INBOUND_ROUTER_ENABLED=True,
        COMPAT_INBOUND_ROUTER_URL=_URL,
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture
def public_dns(monkeypatch):
    async def _fake(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_guard, "_resolve", _fake)


def _install(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def _wrap(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(
        inbound_router,
        "_build_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_wrap)),
    )
    return seen


# --- happy path -------------------------------------------------------------


async def test_posts_contract_body_and_parses_decision(monkeypatch, public_dns):
    seen = _install(monkeypatch, lambda req: httpx.Response(200, json=_OK_BODY))
    result = await inbound_router.route_inbound(
        _settings(), from_number="+15551234567", to_number="+15559999999"
    )
    assert result == InboundRouterResult(
        override_agent_id="agent_deadbeef",
        dynamic_variables={"first_name": "John", "trial_status": "active"},
    )
    # Byte-for-byte request contract (migration spec §3).
    body = json.loads(seen[0].content)
    assert body == {
        "event": "call_inbound",
        "call_inbound": {"from_number": "+15551234567", "to_number": "+15559999999"},
    }


async def test_caller_secret_appended_as_query_param(monkeypatch, public_dns):
    seen = _install(monkeypatch, lambda req: httpx.Response(200, json=_OK_BODY))
    await inbound_router.route_inbound(
        _settings(COMPAT_INBOUND_ROUTER_CALLER_SECRET="s3cr3t"),
        from_number="+1",
        to_number="+1",
    )
    assert "caller_secret=s3cr3t" in str(seen[0].url)


async def test_non_string_dynamic_values_coerced(monkeypatch, public_dns):
    body = {"call_inbound": {"override_agent_id": "agent_x", "dynamic_variables": {"n": 7}}}
    _install(monkeypatch, lambda req: httpx.Response(200, json=body))
    result = await inbound_router.route_inbound(_settings(), from_number="+1", to_number="+1")
    assert result is not None
    assert result.dynamic_variables == {"n": "7"}


# --- disabled / no URL: inert ----------------------------------------------


async def test_none_when_flag_off(monkeypatch, public_dns):
    seen = _install(monkeypatch, lambda req: httpx.Response(200, json=_OK_BODY))
    assert (
        await inbound_router.route_inbound(
            _settings(COMPAT_INBOUND_ROUTER_ENABLED=False), from_number="+1", to_number="+1"
        )
        is None
    )
    assert seen == []  # no egress at all


async def test_none_when_no_url(monkeypatch, public_dns):
    assert (
        await inbound_router.route_inbound(
            _settings(COMPAT_INBOUND_ROUTER_URL=None), from_number="+1", to_number="+1"
        )
        is None
    )


# --- degrade-on-failure -----------------------------------------------------


async def test_degrades_on_non_2xx(monkeypatch, public_dns):
    _install(monkeypatch, lambda req: httpx.Response(500, json={"error": "boom"}))
    assert await inbound_router.route_inbound(_settings(), from_number="+1", to_number="+1") is None


async def test_redirect_is_failure_never_followed(monkeypatch, public_dns):
    _install(
        monkeypatch,
        lambda req: httpx.Response(302, headers={"Location": "https://internal.example/"}),
    )
    assert await inbound_router.route_inbound(_settings(), from_number="+1", to_number="+1") is None


async def test_degrades_on_bad_json(monkeypatch, public_dns):
    _install(monkeypatch, lambda req: httpx.Response(200, content=b"not json"))
    assert await inbound_router.route_inbound(_settings(), from_number="+1", to_number="+1") is None


async def test_degrades_on_missing_call_inbound_wrapper(monkeypatch, public_dns):
    # The legacy flat {agent_id, retell_llm_dynamic_variables} shape is intentionally rejected.
    body = {"agent_id": "agent_x", "retell_llm_dynamic_variables": {"a": "b"}}
    _install(monkeypatch, lambda req: httpx.Response(200, json=body))
    assert await inbound_router.route_inbound(_settings(), from_number="+1", to_number="+1") is None


async def test_degrades_on_blank_override_agent_id(monkeypatch, public_dns):
    body = {"call_inbound": {"override_agent_id": "  ", "dynamic_variables": {}}}
    _install(monkeypatch, lambda req: httpx.Response(200, json=body))
    assert await inbound_router.route_inbound(_settings(), from_number="+1", to_number="+1") is None


async def test_degrades_on_ssrf_block(monkeypatch):
    # Host resolves to a private address → SsrfBlocked → None (fail-closed).
    async def _private(host: str) -> list[str]:
        return ["10.0.0.1"]

    monkeypatch.setattr(ssrf_guard, "_resolve", _private)
    _install(monkeypatch, lambda req: httpx.Response(200, json=_OK_BODY))
    assert await inbound_router.route_inbound(_settings(), from_number="+1", to_number="+1") is None
