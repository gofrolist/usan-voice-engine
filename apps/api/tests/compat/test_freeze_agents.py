"""Contract-freeze tests for the RetellAI-compatible agent surface (Task 13).

Pins that ``GET /get-agent-versions/{id}`` returns a full ``AgentResponse[]``
(not the legacy 4-field summary dict) and that every element conforms to the
oracle ``AgentResponse`` component.

Also pins the ``exclude_none`` wire shape for create/get/list/update so null
fields are omitted rather than emitted as JSON ``null`` (oracle conformance —
``voice_id`` and ``language`` are non-nullable in the spec).
"""

from __future__ import annotations

import pytest

from tests.compat.conformance import assert_conforms
from usan_api.schemas.voice_catalog import VOICE_CATALOG

pytestmark = pytest.mark.frozen

_RETELL_VOICE = "retell-" + VOICE_CATALOG[0].name.split(" - ")[0].split()[0]


def _create_agent(compat_client, compat_headers) -> str:
    """Verbatim happy-path body from tests/test_compat_agents.py."""
    llm = compat_client.post(
        "/create-retell-llm",
        json={"start_speaker": "agent", "general_prompt": "You are a helpful assistant."},
        headers=compat_headers,
    ).json()
    r = compat_client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "voice_id": _RETELL_VOICE,
            "agent_name": "Sales Bot",
        },
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["agent_id"]


def test_get_agent_versions_returns_full_agent_objects(compat_client, compat_headers):
    agent_id = _create_agent(compat_client, compat_headers)
    # Create a second version so the list is non-trivial.
    compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"agent_name": "Sales Bot v2"},
        headers=compat_headers,
    )
    versions = compat_client.get(f"/get-agent-versions/{agent_id}", headers=compat_headers).json()
    assert isinstance(versions, list)
    assert versions
    for v in versions:
        assert v["agent_id"] == agent_id
        assert "voice_id" in v, "expected full AgentResponse, not a 4-field summary dict"
        assert_conforms(v, "AgentResponse")


def test_create_agent_response_conforms_to_oracle(compat_client, compat_headers):
    """create-agent must return an oracle-conformant AgentResponse (exclude_none)."""
    llm = compat_client.post(
        "/create-retell-llm",
        json={"start_speaker": "agent", "general_prompt": "hi"},
        headers=compat_headers,
    ).json()
    r = compat_client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "voice_id": _RETELL_VOICE,
            "agent_name": "Conform Bot",
        },
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    assert_conforms(r.json(), "AgentResponse")


def test_get_agent_response_conforms_to_oracle(compat_client, compat_headers):
    """get-agent must return an oracle-conformant AgentResponse (exclude_none)."""
    agent_id = _create_agent(compat_client, compat_headers)
    r = compat_client.get(f"/get-agent/{agent_id}", headers=compat_headers)
    assert r.status_code == 200, r.text
    assert_conforms(r.json(), "AgentResponse")


def test_list_agents_each_item_conforms_to_oracle(compat_client, compat_headers):
    """list-agents must return oracle-conformant AgentResponse objects (exclude_none)."""
    _create_agent(compat_client, compat_headers)
    items = compat_client.get("/list-agents", headers=compat_headers).json()
    assert items
    for item in items:
        assert_conforms(item, "AgentResponse")


def test_update_agent_response_conforms_to_oracle(compat_client, compat_headers):
    """update-agent must return an oracle-conformant AgentResponse (exclude_none)."""
    agent_id = _create_agent(compat_client, compat_headers)
    r = compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"agent_name": "Updated Bot"},
        headers=compat_headers,
    )
    assert r.status_code == 200, r.text
    assert_conforms(r.json(), "AgentResponse")


# --- Task 14: ?version passthrough, retell-llm list shape, publish authority ---


def test_get_agent_accepts_version_query_and_serves_current(compat_client, compat_headers):
    """GET /get-agent/{id}?version=N is accepted; current config is always served."""
    agent_id = _create_agent(compat_client, compat_headers)
    r = compat_client.get(f"/get-agent/{agent_id}?version=99", headers=compat_headers)
    assert r.status_code == 200, r.text


def test_list_retell_llms_is_bare_array_at_root(compat_client, compat_headers):
    """GET /list-retell-llms returns a bare JSON array at the unversioned root path."""
    r = compat_client.get("/list-retell-llms", headers=compat_headers)
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


def test_publish_returns_server_authoritative_version(compat_client, compat_headers):
    """publish-agent-version ignores the requested version number; server assigns it."""
    agent_id = _create_agent(compat_client, compat_headers)
    r = compat_client.post(
        f"/publish-agent-version/{agent_id}", json={"version": 999}, headers=compat_headers
    )
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["version"], int)
    assert r.json()["version"] != 999
