"""Contract freeze for rerun-chat-analysis (RetellAI parity Phase 4c-2).

The rerun response is a ChatResponse carrying chat_analysis; it must conform to the pinned
oracle schema and round-trip through the retell SDK model. Vertex is mocked (no real LLM).
"""

from __future__ import annotations

import json

import pytest

from usan_api.vertex_test import VertexTurn

from .conformance import assert_conforms, assert_sdk_roundtrip


@pytest.fixture
def mock_vertex_analysis(monkeypatch):
    async def _reply(**kwargs):
        return VertexTurn(text="agent reply")

    async def _analysis(**kwargs):
        return VertexTurn(
            text=json.dumps(
                {
                    "chat_summary": "The agent answered the user's question.",
                    "user_sentiment": "Positive",
                    "chat_successful": True,
                }
            )
        )

    monkeypatch.setattr("usan_api.compat.chat_service.run_vertex_turn", _reply)
    monkeypatch.setattr("usan_api.chat_analysis.run_vertex_turn", _analysis)


def test_rerun_chat_analysis_conforms(
    compat_client, compat_headers, web_agent_id, chat_analysis_on, mock_vertex_analysis
):
    chat_id = compat_client.post(
        "/create-chat", json={"agent_id": web_agent_id}, headers=compat_headers
    ).json()["chat_id"]
    assert (
        compat_client.post(
            "/create-chat-completion",
            json={"chat_id": chat_id, "content": "what time is it?"},
            headers=compat_headers,
        ).status_code
        == 201
    )

    r = compat_client.put(f"/rerun-chat-analysis/{chat_id}", headers=compat_headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["chat_id"] == chat_id
    assert body["chat_analysis"]["chat_summary"]
    assert body["chat_analysis"]["user_sentiment"] == "Positive"
    assert body["chat_analysis"]["chat_successful"] is True
    assert_conforms(body, "ChatResponse")
    assert_sdk_roundtrip(body, "retell.types:ChatResponse")
