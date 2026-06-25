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
