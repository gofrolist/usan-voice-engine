"""Phase 4c-1: a chat agent never leaks into the voice surfaces; retell-llm stays agnostic."""

from __future__ import annotations

from tests.compat.conftest import RETELL_VOICE


def _make_chat(compat_client, compat_headers) -> dict:
    llm = compat_client.post(
        "/create-retell-llm", json={"general_prompt": "chat"}, headers=compat_headers
    ).json()
    r = compat_client.post(
        "/create-chat-agent",
        json={"response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]}},
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    return {"agent_id": r.json()["agent_id"], "llm_id": llm["llm_id"]}


def _make_voice(compat_client, compat_headers) -> str:
    llm = compat_client.post(
        "/create-retell-llm", json={"general_prompt": "voice"}, headers=compat_headers
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
    return r.json()["agent_id"]


def test_voice_list_agents_excludes_chat(compat_client, compat_headers):
    chat = _make_chat(compat_client, compat_headers)
    voice_id = _make_voice(compat_client, compat_headers)
    items = compat_client.get("/list-agents", headers=compat_headers).json()
    ids = {i["agent_id"] for i in items}
    assert voice_id in ids
    assert chat["agent_id"] not in ids


def test_v2_list_agents_excludes_chat(compat_client, compat_headers):
    chat = _make_chat(compat_client, compat_headers)
    body = compat_client.post("/v2/list-agents", json={}, headers=compat_headers).json()
    ids = {i["agent_id"] for i in body["items"]}
    assert chat["agent_id"] not in ids


def test_list_retell_llms_includes_chat_bound_llm(compat_client, compat_headers):
    """A Retell-LLM is channel-agnostic infra: list-retell-llms must still show a chat-bound LLM."""
    chat = _make_chat(compat_client, compat_headers)
    llms = compat_client.get("/list-retell-llms", headers=compat_headers).json()
    assert any(item["llm_id"] == chat["llm_id"] for item in llms)


def test_get_retell_llm_works_on_chat_bound(compat_client, compat_headers):
    chat = _make_chat(compat_client, compat_headers)
    r = compat_client.get(f"/get-retell-llm/{chat['llm_id']}", headers=compat_headers)
    assert r.status_code == 200, r.text


def test_get_chat_agent_404s_on_voice_id(compat_client, compat_headers):
    voice_id = _make_voice(compat_client, compat_headers)
    r = compat_client.get(f"/get-chat-agent/{voice_id}", headers=compat_headers)
    assert r.status_code == 404
