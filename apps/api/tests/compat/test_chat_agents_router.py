"""Phase 4c-1: chat-agent router happy paths + cross-resource isolation."""

from __future__ import annotations

from tests.compat.conftest import RETELL_VOICE


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
            "agent_name": "Chat Bot",
        },
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_create_get_roundtrip(compat_client, compat_headers):
    created = _create_chat_agent(compat_client, compat_headers)
    agent_id = created["agent_id"]
    got = compat_client.get(f"/get-chat-agent/{agent_id}", headers=compat_headers)
    assert got.status_code == 200, got.text
    assert got.json()["agent_id"] == agent_id


def test_list_chat_agents_returns_only_chat(compat_client, compat_headers):
    created = _create_chat_agent(compat_client, compat_headers)
    items = compat_client.get("/list-chat-agents", headers=compat_headers).json()
    assert isinstance(items, list)
    assert any(i["agent_id"] == created["agent_id"] for i in items)


def test_delete_then_404(compat_client, compat_headers):
    agent_id = _create_chat_agent(compat_client, compat_headers)["agent_id"]
    del_r = compat_client.delete(f"/delete-chat-agent/{agent_id}", headers=compat_headers)
    assert del_r.status_code == 204
    get_r = compat_client.get(f"/get-chat-agent/{agent_id}", headers=compat_headers)
    assert get_r.status_code == 404


def test_get_agent_404s_on_chat_id(compat_client, compat_headers):
    """Cross-resource isolation: the VOICE get-agent op must 404 on a chat-agent id."""
    agent_id = _create_chat_agent(compat_client, compat_headers)["agent_id"]
    assert compat_client.get(f"/get-agent/{agent_id}", headers=compat_headers).status_code == 404


def test_publish_chat_agent_200(compat_client, compat_headers):
    agent_id = _create_chat_agent(compat_client, compat_headers)["agent_id"]
    r = compat_client.post(f"/publish-chat-agent/{agent_id}", headers=compat_headers)
    assert r.status_code == 200, r.text


def test_create_chat_agent_rejects_published_voice_llm_id(compat_client, compat_headers):
    """Bind-hijack guard (a): a published VOICE agent's llm_id must 409 on create-chat-agent."""
    # Step 1: create-retell-llm -> create-agent (publishes a voice agent)
    llm = compat_client.post(
        "/create-retell-llm",
        json={"general_prompt": "voice prompt"},
        headers=compat_headers,
    ).json()
    voice_r = compat_client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "voice_id": RETELL_VOICE,
            "agent_name": "Voice Agent For Chat Hijack Test",
        },
        headers=compat_headers,
    )
    assert voice_r.status_code == 201, voice_r.text
    # The voice agent is now published (published_version is not None, channel='voice').
    # Step 2: attempt to bind the same llm_id to a chat agent — must 409
    chat_r = compat_client.post(
        "/create-chat-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "agent_name": "Hijack Attempt",
        },
        headers=compat_headers,
    )
    assert chat_r.status_code == 409, chat_r.text


def test_create_agent_rejects_published_chat_llm_id(compat_client, compat_headers):
    """Bind-hijack guard (b): a published CHAT agent's llm_id must 409 on create-agent."""
    # Step 1: create-retell-llm -> create-chat-agent (publishes a chat agent)
    llm = compat_client.post(
        "/create-retell-llm",
        json={"general_prompt": "chat prompt"},
        headers=compat_headers,
    ).json()
    chat_r = compat_client.post(
        "/create-chat-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "agent_name": "Chat Agent For Voice Hijack Test",
        },
        headers=compat_headers,
    )
    assert chat_r.status_code == 201, chat_r.text
    # The chat agent is now published (published_version is not None, channel='chat').
    # Step 2: attempt to bind the same llm_id to a voice agent — must 409
    voice_r = compat_client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "voice_id": RETELL_VOICE,
            "agent_name": "Voice Hijack Attempt",
        },
        headers=compat_headers,
    )
    assert voice_r.status_code == 409, voice_r.text
