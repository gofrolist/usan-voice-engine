"""WS-B: create/update-retell-llm ingests general_tools into config.tools.external_tools.

Driven over the mounted compat sub-app (compat_client + compat_headers). The feature is
flag-gated: COMPAT_EXTERNAL_TOOLS_ENABLED off keeps general_tools echo-only (no new 422);
on, custom tools are translated + persisted and the egress allow-list is enforced at save.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from .test_compat_agents import _create_llm, _fetch_draft_config

_ALLOWED_HOST = "client.example.com"
_PARAMS = {"type": "object", "properties": {"phone": {"type": "string"}}, "required": ["phone"]}


def _custom_tool(url_host=_ALLOWED_HOST, name="schedule_callback"):
    return {
        "type": "custom",
        "name": name,
        "description": "Schedule a callback.",
        "url": f"https://{url_host}/functions/v1/schedule-callback",
        "method": "POST",
        "parameters": _PARAMS,
    }


@pytest.fixture
def enable_external_tools(monkeypatch):
    """Turn the feature on with an allow-listed host, mirroring allow_webhook_host."""
    from usan_api.settings import get_settings

    monkeypatch.setenv("COMPAT_EXTERNAL_TOOLS_ENABLED", "true")
    monkeypatch.setenv("COMPAT_TOOL_ALLOWED_HOSTS", _ALLOWED_HOST)
    monkeypatch.setenv("COMPAT_TOOL_CALLER_SECRET", "test-caller-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _profile_id(llm_json) -> uuid.UUID:
    return uuid.UUID(hex=llm_json["llm_id"][len("llm_") :])


# --- enabled: translate + persist -------------------------------------------


def test_custom_tool_persisted_when_enabled(
    compat_client, compat_headers, enable_external_tools, async_database_url
):
    # `schedule_callback` is also a Clara builtin (enabled by default). The client's external
    # tool shadows it: it lands in external_tools and is dropped from enabled (no double-reg).
    r = _create_llm(compat_client, compat_headers, general_tools=[_custom_tool()])
    assert r.status_code == 201, r.text
    cfg = asyncio.run(_fetch_draft_config(async_database_url, _profile_id(r.json())))
    ext = cfg["tools"]["external_tools"]
    assert [t["name"] for t in ext] == ["schedule_callback"]
    assert ext[0]["url"] == f"https://{_ALLOWED_HOST}/functions/v1/schedule-callback"
    assert "schedule_callback" not in cfg["tools"]["enabled"]


def test_raw_general_tools_still_echoed(compat_client, compat_headers, enable_external_tools):
    # Parity §0: the submitted general_tools list round-trips verbatim via compat_extras even
    # though it is ALSO translated into executable specs.
    llm = _create_llm(compat_client, compat_headers, general_tools=[_custom_tool()]).json()
    got = compat_client.get(f"/get-retell-llm/{llm['llm_id']}", headers=compat_headers).json()
    assert got.get("general_tools") == [_custom_tool()]


def test_disallowed_host_rejected_when_enabled(
    compat_client, compat_headers, enable_external_tools
):
    r = _create_llm(
        compat_client, compat_headers, general_tools=[_custom_tool(url_host="evil.example.net")]
    )
    assert r.status_code == 422, r.text
    assert "allow-list" in r.json()["message"]


def test_external_tool_shadows_same_named_builtin(
    compat_client, compat_headers, enable_external_tools, async_database_url
):
    # Declaring an external raise_crisis (a safety builtin) does not 422 — the client's
    # function shadows the builtin: raise_crisis is dropped from enabled, added to external.
    r = _create_llm(
        compat_client, compat_headers, general_tools=[_custom_tool(name="raise_crisis")]
    )
    assert r.status_code == 201, r.text
    cfg = asyncio.run(_fetch_draft_config(async_database_url, _profile_id(r.json())))
    assert "raise_crisis" not in cfg["tools"]["enabled"]
    assert [t["name"] for t in cfg["tools"]["external_tools"]] == ["raise_crisis"]


def test_kb_lookup_not_persisted_as_tool(
    compat_client, compat_headers, enable_external_tools, async_database_url
):
    kb = {
        "name": "kb_lookup",
        "url": "RETELL_BUILT_IN — handled natively when the KB is uploaded",
        "parameters": {"type": "object", "properties": {}},
    }
    r = _create_llm(compat_client, compat_headers, general_tools=[kb])
    assert r.status_code == 201, r.text
    cfg = asyncio.run(_fetch_draft_config(async_database_url, _profile_id(r.json())))
    assert cfg["tools"]["external_tools"] == []


# --- disabled: echo-only, inert ---------------------------------------------


def test_echo_only_when_disabled(compat_client, compat_headers, async_database_url):
    # Flag off (default): a tool on a NON-allow-listed host is accepted (no 422) and NOT
    # translated — pre-Surface-3 behavior is unchanged.
    r = _create_llm(
        compat_client, compat_headers, general_tools=[_custom_tool(url_host="evil.example.net")]
    )
    assert r.status_code == 201, r.text
    cfg = asyncio.run(_fetch_draft_config(async_database_url, _profile_id(r.json())))
    assert cfg["tools"]["external_tools"] == []
