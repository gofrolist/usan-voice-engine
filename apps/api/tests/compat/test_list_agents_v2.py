"""Frozen: POST /v2/list-agents returns a paginated wrapper of AgentListItemResponse.

Oracle (openapi-final.yaml:12869-12907): response is allOf[PaginatedResponseBase,
{items: array<AgentListItemResponse>}].  The wrapper key is ``items`` (confirmed line 12896).

The retell-sdk AgentListResponse is List[AgentResponse] (bare array, no paginated wrapper
model).  There is no SDK model for the v2 paginated response, so assert_sdk_roundtrip is
not used here; correctness is verified via explicit field assertions against the oracle shape.
"""

from __future__ import annotations

import pytest

from tests.compat.conftest import _published_agent_id

pytestmark = pytest.mark.frozen


def test_list_agents_v2_is_paginated_listitems(compat_client, compat_headers):
    """A seeded published agent appears in the items list with the correct oracle shape."""
    _published_agent_id(compat_client, compat_headers)
    resp = compat_client.post("/v2/list-agents", json={}, headers=compat_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Wrapper must contain the oracle-specified key.
    assert "items" in body, f"expected 'items' key in response, got: {list(body.keys())}"
    assert "has_more" in body

    items = body["items"]
    assert len(items) >= 1, "expected at least one agent in items"

    item = items[0]
    required_fields = {"agent_id", "agent_name", "channel", "user_modified_timestamp", "tags"}
    assert required_fields <= item.keys(), (
        f"missing required AgentListItemResponse fields: {required_fields - item.keys()}"
    )
    assert item["channel"] == "voice"
    assert isinstance(item["user_modified_timestamp"], int)
    assert item["user_modified_timestamp"] > 0
    assert isinstance(item["tags"], dict)
    assert body["has_more"] is False


def test_list_agents_v2_filter_by_channel_chat_is_empty(compat_client, compat_headers):
    """channel=chat filter always returns an empty list (voice-only engine)."""
    _published_agent_id(compat_client, compat_headers)
    resp = compat_client.post(
        "/v2/list-agents",
        json={"filter_criteria": {"channel": {"type": "string", "op": "eq", "value": "chat"}}},
        headers=compat_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


def test_list_agents_v2_filter_by_channel_voice_returns_all(compat_client, compat_headers):
    """channel=voice filter keeps all agents (same as no filter)."""
    _published_agent_id(compat_client, compat_headers)
    resp = compat_client.post(
        "/v2/list-agents",
        json={"filter_criteria": {"channel": {"type": "string", "op": "eq", "value": "voice"}}},
        headers=compat_headers,
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["items"]) >= 1


def test_list_agents_v2_empty_body_is_200(compat_client, compat_headers):
    """Empty JSON body (no filter_criteria) is valid — requestBody is not required."""
    _published_agent_id(compat_client, compat_headers)
    resp = compat_client.post("/v2/list-agents", json={}, headers=compat_headers)
    assert resp.status_code == 200, resp.text
    assert "items" in resp.json()


def test_list_agents_v2_query_matches_agent_id(compat_client, compat_headers):
    """FROZEN: query filter must match agent_id substring (oracle AgentListFilter conformance).

    Oracle (~1350-1352): 'Case-insensitive substring search over agent name, PLUS
    substring search over agent id.'  Take the first 8 chars of the hex portion of the
    encoded agent_id (after the 'agent_' prefix), lowercased — that substring must match.
    """
    agent_id = _published_agent_id(compat_client, compat_headers)
    # agent_id is e.g. "agent_<32 hex chars>"
    # Take chars 6-14 (first 8 hex chars after "agent_") for a distinctive substring.
    id_substr = agent_id[len("agent_") : len("agent_") + 8].lower()

    resp = compat_client.post(
        "/v2/list-agents",
        json={"filter_criteria": {"query": id_substr}},
        headers=compat_headers,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    matched_ids = [item["agent_id"] for item in items]
    assert agent_id in matched_ids, (
        f"Expected agent_id {agent_id!r} to appear when querying by id-substr {id_substr!r}; "
        f"got: {matched_ids}"
    )


def test_list_agents_v2_agent_name_always_str(compat_client, compat_headers):
    """FROZEN: agent_name is oracle-REQUIRED in AgentListItemResponse and must always be a str.

    Oracle AgentListItemResponse.required includes agent_name.  The serializer must never
    emit null or omit the field (exclude_none would drop a null, violating the required
    constraint).
    """
    _published_agent_id(compat_client, compat_headers)
    resp = compat_client.post("/v2/list-agents", json={}, headers=compat_headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) >= 1, "expected at least one item"
    for item in items:
        assert "agent_name" in item, f"agent_name missing from item: {item}"
        assert isinstance(item["agent_name"], str), (
            f"agent_name must be str, got {type(item['agent_name'])!r}: {item['agent_name']!r}"
        )
