"""Contract-freeze tests for the RetellAI-compatible chat-agent surface (Phase 4c-1).

Pins that create/get/list/version chat-agent responses conform to the oracle ChatAgentResponse
component AND round-trip through retell-sdk 5.53.0's ChatAgentResponse model.
"""

from __future__ import annotations

import pytest

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip

pytestmark = pytest.mark.frozen


def _create_chat_agent(compat_client, compat_headers) -> dict:
    llm = compat_client.post(
        "/create-retell-llm",
        json={"general_prompt": "You are a helpful chat assistant."},
        headers=compat_headers,
    ).json()
    r = compat_client.post(
        "/create-chat-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "agent_name": "Freeze Chat Bot",
        },
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_create_chat_agent_conforms(compat_client, compat_headers):
    payload = _create_chat_agent(compat_client, compat_headers)
    assert_conforms(payload, "ChatAgentResponse")
    assert_sdk_roundtrip(payload, "retell.types:ChatAgentResponse")
    assert "base_version" not in payload  # optional, omitted via exclude_none
    assert "assigned_tags" not in payload


def test_get_chat_agent_conforms(compat_client, compat_headers):
    agent_id = _create_chat_agent(compat_client, compat_headers)["agent_id"]
    r = compat_client.get(f"/get-chat-agent/{agent_id}", headers=compat_headers)
    assert r.status_code == 200, r.text
    assert_conforms(r.json(), "ChatAgentResponse")
    assert_sdk_roundtrip(r.json(), "retell.types:ChatAgentResponse")


def test_list_chat_agents_items_conform(compat_client, compat_headers):
    _create_chat_agent(compat_client, compat_headers)
    items = compat_client.get("/list-chat-agents", headers=compat_headers).json()
    assert items
    for item in items:
        assert_conforms(item, "ChatAgentResponse")
        assert_sdk_roundtrip(item, "retell.types:ChatAgentResponse")


def test_get_chat_agent_versions_conform(compat_client, compat_headers):
    agent_id = _create_chat_agent(compat_client, compat_headers)["agent_id"]
    compat_client.patch(
        f"/update-chat-agent/{agent_id}", json={"agent_name": "v2"}, headers=compat_headers
    )
    versions = compat_client.get(
        f"/get-chat-agent-versions/{agent_id}", headers=compat_headers
    ).json()
    assert isinstance(versions, list)
    assert versions
    for v in versions:
        assert v["agent_id"] == agent_id
        assert_conforms(v, "ChatAgentResponse")
        assert_sdk_roundtrip(v, "retell.types:ChatAgentResponse")
