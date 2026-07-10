"""Phase 4c-2: PUT /rerun-chat-analysis behavior — auth, 404, populate, get/list reflect."""

from __future__ import annotations

import json
import uuid

import pytest

from usan_api.compat import ids
from usan_api.vertex_test import VertexTurn


@pytest.fixture
def patched_vertex(monkeypatch):
    """Mock BOTH Vertex seams: chat_service (create-chat-completion turn) and chat_analysis."""

    async def _reply(**kwargs):
        return VertexTurn(text="agent reply")

    async def _analysis(**kwargs):
        return VertexTurn(
            text=json.dumps(
                {
                    "chat_summary": "A friendly check-in.",
                    "user_sentiment": "Positive",
                    "chat_successful": True,
                }
            )
        )

    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", _reply)
    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _analysis)


def _seed_chat(compat_client, compat_headers, web_agent_id) -> str:
    chat = compat_client.post(
        "/create-chat", json={"agent_id": web_agent_id}, headers=compat_headers
    ).json()
    chat_id = chat["chat_id"]
    # One completion turn so the chat has messages to analyze.
    r = compat_client.post(
        "/create-chat-completion",
        json={"chat_id": chat_id, "content": "hi there"},
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    return chat_id


def test_rerun_unknown_chat_404(compat_client, compat_headers, chat_analysis_on):
    r = compat_client.put(
        f"/rerun-chat-analysis/{ids.encode_chat_id(uuid.uuid4())}", headers=compat_headers
    )
    assert r.status_code == 404


def test_rerun_populates_and_get_reflects(
    compat_client, compat_headers, web_agent_id, chat_analysis_on, patched_vertex
):
    chat_id = _seed_chat(compat_client, compat_headers, web_agent_id)

    r = compat_client.put(f"/rerun-chat-analysis/{chat_id}", headers=compat_headers)
    assert r.status_code == 201, r.text
    analysis = r.json()["chat_analysis"]
    assert analysis["chat_summary"] == "A friendly check-in."
    assert analysis["user_sentiment"] == "Positive"
    assert analysis["chat_successful"] is True

    # get-chat now reflects the stored analysis.
    got = compat_client.get(f"/get-chat/{chat_id}", headers=compat_headers).json()
    assert got["chat_analysis"]["chat_summary"] == "A friendly check-in."

    # list-chats reflects it too (batched load).
    listing = compat_client.post(
        "/v3/list-chats", json={"limit": 50}, headers=compat_headers
    ).json()
    mine = next(c for c in listing["items"] if c["chat_id"] == chat_id)
    assert mine["chat_analysis"]["user_sentiment"] == "Positive"


def test_rerun_archived_chat_404(compat_client, compat_headers, web_agent_id, chat_analysis_on):
    chat = compat_client.post(
        "/create-chat", json={"agent_id": web_agent_id}, headers=compat_headers
    ).json()
    chat_id = chat["chat_id"]
    del_r = compat_client.delete(f"/delete-chat/{chat_id}", headers=compat_headers)
    assert del_r.status_code == 204
    r = compat_client.put(f"/rerun-chat-analysis/{chat_id}", headers=compat_headers)
    assert r.status_code == 404
