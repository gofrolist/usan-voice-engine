"""Contract freeze for the api_chat chat-session surface (RetellAI parity Phase 4a)."""

from __future__ import annotations

import pytest

from usan_api.vertex_test import VertexTurn

from .conformance import assert_conforms, assert_sdk_roundtrip


@pytest.fixture
def mock_vertex(monkeypatch):
    """Stub the Vertex turn so the freeze suite places no real LLM call."""

    async def _fake(**kwargs):
        assert kwargs["tools"] == []
        return VertexTurn(text="hello from the agent")

    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", _fake)


def _create_chat(client, headers, agent_id, **overrides):
    body = {"agent_id": agent_id}
    body.update(overrides)
    return client.post("/create-chat", json=body, headers=headers)


def test_create_chat_conforms(compat_client, compat_headers, web_agent_id):
    r = _create_chat(
        compat_client,
        compat_headers,
        web_agent_id,
        metadata={"crm": "x"},
        retell_llm_dynamic_variables={"name": "Pat"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["chat_status"] == "ongoing"
    assert body["chat_type"] == "api_chat"
    assert isinstance(body["chat_id"], str)
    assert body["chat_id"].startswith("chat_")
    assert isinstance(body["agent_id"], str)
    assert isinstance(body["version"], int)
    assert_conforms(body, "ChatResponse")
    assert_sdk_roundtrip(body, "retell.types:ChatResponse")


def test_create_chat_rejects_malformed_agent(compat_client, compat_headers):
    assert _create_chat(compat_client, compat_headers, "not-an-agent").status_code == 422


def test_create_chat_rejects_unpublished_agent(compat_client, compat_headers):
    assert _create_chat(compat_client, compat_headers, "agent_" + "0" * 32).status_code == 422


def test_create_chat_rejects_reserved_prefix(compat_client, compat_headers, web_agent_id):
    r = _create_chat(
        compat_client,
        compat_headers,
        web_agent_id,
        retell_llm_dynamic_variables={"__meta__x": "1"},
    )
    assert r.status_code == 422


def test_completion_conforms_and_returns_agent_message(
    compat_client, compat_headers, web_agent_id, mock_vertex, gcp_project_set
):
    chat = _create_chat(compat_client, compat_headers, web_agent_id).json()
    r = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat["chat_id"], "content": "hi"},
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert [m["role"] for m in body["messages"]] == ["agent"]
    assert body["messages"][0]["content"] == "hello from the agent"
    for m in body["messages"]:
        assert_conforms(m, "Message")
    assert_sdk_roundtrip(body, "retell.types:ChatCreateChatCompletionResponse")


def test_completion_503_when_gcp_unset(
    compat_client, compat_headers, web_agent_id, mock_vertex, gcp_project_unset
):
    chat = _create_chat(compat_client, compat_headers, web_agent_id).json()
    r = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat["chat_id"], "content": "hi"},
        headers=compat_headers,
    )
    assert r.status_code == 503


def test_get_chat_includes_transcript(
    compat_client, compat_headers, web_agent_id, mock_vertex, gcp_project_set
):
    chat = _create_chat(compat_client, compat_headers, web_agent_id).json()
    compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat["chat_id"], "content": "hi"},
        headers=compat_headers,
    )
    got = compat_client.get(f"/get-chat/{chat['chat_id']}", headers=compat_headers).json()
    assert got["message_with_tool_calls"]
    assert "transcript" in got
    assert_conforms(got, "ChatResponse")
    assert_sdk_roundtrip(got, "retell.types:ChatResponse")


def test_end_then_completion_is_422(
    compat_client, compat_headers, web_agent_id, gcp_project_set, mock_vertex
):
    chat = _create_chat(compat_client, compat_headers, web_agent_id).json()
    assert (
        compat_client.patch(f"/end-chat/{chat['chat_id']}", headers=compat_headers).status_code
        == 204
    )
    got = compat_client.get(f"/get-chat/{chat['chat_id']}", headers=compat_headers).json()
    assert got["chat_status"] == "ended"
    r = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat["chat_id"], "content": "again"},
        headers=compat_headers,
    )
    assert r.status_code == 422


def test_delete_then_get_is_404(compat_client, compat_headers, web_agent_id):
    chat = _create_chat(compat_client, compat_headers, web_agent_id).json()
    assert (
        compat_client.delete(f"/delete-chat/{chat['chat_id']}", headers=compat_headers).status_code
        == 204
    )
    assert (
        compat_client.get(f"/get-chat/{chat['chat_id']}", headers=compat_headers).status_code == 404
    )


def test_list_chats_items_omit_transcript_and_conform(compat_client, compat_headers, web_agent_id):
    _create_chat(
        compat_client, compat_headers, web_agent_id, retell_llm_dynamic_variables={"name": "Pat"}
    )
    r = compat_client.post(
        "/v3/list-chats", json={"limit": 10, "include_total": True}, headers=compat_headers
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body["items"], list)
    assert body["items"]
    for item in body["items"]:
        assert "transcript" not in item
        assert "message_with_tool_calls" not in item
        assert_conforms(item, "V3ChatResponse")
    assert_sdk_roundtrip(body, "retell.types:ChatListResponse")


def test_update_chat_round_trips_metadata(compat_client, compat_headers, web_agent_id):
    chat = _create_chat(compat_client, compat_headers, web_agent_id).json()
    upd = compat_client.patch(
        f"/update-chat/{chat['chat_id']}",
        json={"metadata": {"crm": "y"}, "override_dynamic_variables": {"name": "Bo"}},
        headers=compat_headers,
    )
    assert upd.status_code == 200, upd.text
    body = upd.json()
    assert body["metadata"] == {"crm": "y"}
    assert body["retell_llm_dynamic_variables"] == {"name": "Bo"}
    assert_conforms(body, "ChatResponse")
