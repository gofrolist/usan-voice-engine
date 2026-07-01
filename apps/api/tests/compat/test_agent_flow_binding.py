"""create/update-agent conversation-flow binding fidelity (Phase 6c)."""

from __future__ import annotations

import uuid

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip
from tests.compat.conftest import RETELL_VOICE
from usan_api.compat import ids

_FLOW = {
    "start_speaker": "agent",
    "model_choice": {"type": "cascading", "model": "gpt-4.1"},
    "nodes": [],
}


def _create_flow(compat_client, compat_headers) -> str:
    r = compat_client.post("/create-conversation-flow", json=_FLOW, headers=compat_headers)
    assert r.status_code == 201, r.text
    return r.json()["conversation_flow_id"]


def _create_flow_agent(compat_client, compat_headers, flow_id, **extra):
    body = {
        "response_engine": {"type": "conversation-flow", "conversation_flow_id": flow_id},
        "voice_id": RETELL_VOICE,
        "agent_name": "Flow Bot",
        **extra,
    }
    return compat_client.post("/create-agent", json=body, headers=compat_headers)


def test_create_conversation_flow_agent(compat_client, compat_headers):
    flow_id = _create_flow(compat_client, compat_headers)
    r = _create_flow_agent(compat_client, compat_headers, flow_id)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["response_engine"] == {"type": "conversation-flow", "conversation_flow_id": flow_id}
    assert_conforms(body, "AgentResponse")
    assert_sdk_roundtrip(body, "retell.types:AgentResponse")


def test_get_and_list_echo_flow_variant(compat_client, compat_headers):
    flow_id = _create_flow(compat_client, compat_headers)
    agent_id = _create_flow_agent(compat_client, compat_headers, flow_id).json()["agent_id"]
    got = compat_client.get(f"/get-agent/{agent_id}", headers=compat_headers).json()
    assert got["response_engine"]["type"] == "conversation-flow"
    assert got["response_engine"]["conversation_flow_id"] == flow_id
    listed = compat_client.get("/list-agents", headers=compat_headers).json()
    match = [a for a in listed if a["agent_id"] == agent_id]
    assert match
    assert match[0]["response_engine"]["conversation_flow_id"] == flow_id


def test_create_flow_agent_echoes_version(compat_client, compat_headers):
    flow_id = _create_flow(compat_client, compat_headers)
    r = _create_flow_agent(
        compat_client,
        compat_headers,
        flow_id,
        response_engine={
            "type": "conversation-flow",
            "conversation_flow_id": flow_id,
            "version": 2,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["response_engine"]["version"] == 2


def test_create_flow_agent_missing_flow_id_is_422(compat_client, compat_headers):
    r = compat_client.post(
        "/create-agent",
        json={"response_engine": {"type": "conversation-flow"}, "voice_id": RETELL_VOICE},
        headers=compat_headers,
    )
    assert r.status_code == 422, r.text


def test_create_flow_agent_unknown_flow_is_422(compat_client, compat_headers):
    bogus = ids.encode_conversation_flow_id(uuid.uuid4())
    r = _create_flow_agent(compat_client, compat_headers, bogus)
    assert r.status_code == 422, r.text


def test_create_flow_agent_malformed_flow_is_422(compat_client, compat_headers):
    r = _create_flow_agent(compat_client, compat_headers, "not-a-flow-id")
    assert r.status_code == 422, r.text


def test_create_custom_llm_agent_is_422(compat_client, compat_headers):
    r = compat_client.post(
        "/create-agent",
        json={
            "response_engine": {
                "type": "custom-llm",
                "llm_websocket_url": "wss://evil.example/llm",
            },
            "voice_id": RETELL_VOICE,
        },
        headers=compat_headers,
    )
    assert r.status_code == 422, r.text


def test_create_retell_llm_agent_still_works(compat_client, compat_headers):
    llm = compat_client.post(
        "/create-retell-llm",
        json={"start_speaker": "agent", "general_prompt": "hi"},
        headers=compat_headers,
    ).json()
    r = compat_client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "voice_id": RETELL_VOICE,
        },
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["response_engine"] == {"type": "retell-llm", "llm_id": llm["llm_id"]}


def _create_retell_agent(compat_client, compat_headers) -> tuple[str, str]:
    llm = compat_client.post(
        "/create-retell-llm",
        json={"start_speaker": "agent", "general_prompt": "hi"},
        headers=compat_headers,
    ).json()
    agent = compat_client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "voice_id": RETELL_VOICE,
        },
        headers=compat_headers,
    ).json()
    return agent["agent_id"], llm["llm_id"]


def test_update_switches_llm_agent_to_flow(compat_client, compat_headers):
    agent_id, _ = _create_retell_agent(compat_client, compat_headers)
    flow_id = _create_flow(compat_client, compat_headers)
    r = compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"response_engine": {"type": "conversation-flow", "conversation_flow_id": flow_id}},
        headers=compat_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["response_engine"] == {
        "type": "conversation-flow",
        "conversation_flow_id": flow_id,
    }
    assert_conforms(r.json(), "AgentResponse")


def test_update_repoints_flow_agent_to_another_flow(compat_client, compat_headers):
    flow_a = _create_flow(compat_client, compat_headers)
    flow_b = _create_flow(compat_client, compat_headers)
    agent_id = _create_flow_agent(compat_client, compat_headers, flow_a).json()["agent_id"]
    r = compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"response_engine": {"type": "conversation-flow", "conversation_flow_id": flow_b}},
        headers=compat_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["response_engine"]["conversation_flow_id"] == flow_b


def test_update_reverts_flow_agent_to_self_llm(compat_client, compat_headers):
    agent_id, llm_id = _create_retell_agent(compat_client, compat_headers)
    flow_id = _create_flow(compat_client, compat_headers)
    compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"response_engine": {"type": "conversation-flow", "conversation_flow_id": flow_id}},
        headers=compat_headers,
    )
    r = compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"response_engine": {"type": "retell-llm", "llm_id": llm_id}},
        headers=compat_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["response_engine"] == {"type": "retell-llm", "llm_id": llm_id}


def test_update_foreign_llm_id_is_409(compat_client, compat_headers):
    agent_a, _ = _create_retell_agent(compat_client, compat_headers)
    _, llm_b = _create_retell_agent(compat_client, compat_headers)
    r = compat_client.patch(
        f"/update-agent/{agent_a}",
        json={"response_engine": {"type": "retell-llm", "llm_id": llm_b}},
        headers=compat_headers,
    )
    assert r.status_code == 409, r.text


def test_update_unknown_flow_is_422(compat_client, compat_headers):
    agent_id, _ = _create_retell_agent(compat_client, compat_headers)
    bogus = ids.encode_conversation_flow_id(uuid.uuid4())
    r = compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"response_engine": {"type": "conversation-flow", "conversation_flow_id": bogus}},
        headers=compat_headers,
    )
    assert r.status_code == 422, r.text


def test_update_custom_llm_is_422(compat_client, compat_headers):
    agent_id, _ = _create_retell_agent(compat_client, compat_headers)
    r = compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"response_engine": {"type": "custom-llm", "llm_websocket_url": "wss://x/y"}},
        headers=compat_headers,
    )
    assert r.status_code == 422, r.text


def test_update_without_response_engine_preserves_flow_binding(compat_client, compat_headers):
    """A PATCH that omits response_engine (e.g. voice-only) keeps the existing flow binding."""
    flow_id = _create_flow(compat_client, compat_headers)
    agent_id = _create_flow_agent(compat_client, compat_headers, flow_id).json()["agent_id"]
    r = compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"voice_id": RETELL_VOICE},
        headers=compat_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["response_engine"] == {
        "type": "conversation-flow",
        "conversation_flow_id": flow_id,
    }


def test_create_retell_llm_rebind_of_flow_agent_own_id_yields_retell_llm(
    compat_client, compat_headers
):
    """Re-binding a flow agent's own UUID (re-encoded as an llm_id) via create-agent produces a
    genuine retell-llm agent — the stale flow key is cleared (symmetry with update-agent)."""
    flow_id = _create_flow(compat_client, compat_headers)
    agent_id = _create_flow_agent(compat_client, compat_headers, flow_id).json()["agent_id"]
    self_llm_id = ids.encode_llm_id(ids.decode_agent_id(agent_id))
    r = compat_client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": self_llm_id},
            "voice_id": RETELL_VOICE,
        },
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["response_engine"] == {"type": "retell-llm", "llm_id": self_llm_id}
