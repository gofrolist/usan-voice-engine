"""Unit tests for agent_bridge._response_engine variant derivation (Phase 6c)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from usan_api.compat import agent_bridge, ids


def _profile(config: dict) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), draft_config=config)


def test_response_engine_defaults_to_retell_llm_self_view():
    p = _profile({"prompts": {}, "voice": {}})
    eng = agent_bridge._response_engine(p)
    assert eng == {"type": "retell-llm", "llm_id": ids.encode_llm_id(p.id)}


def test_response_engine_conversation_flow_with_version():
    token = ids.encode_conversation_flow_id(uuid.uuid4())
    p = _profile(
        {
            "compat_response_engine": {
                "type": "conversation-flow",
                "conversation_flow_id": token,
                "version": 3,
            }
        }
    )
    assert agent_bridge._response_engine(p) == {
        "type": "conversation-flow",
        "conversation_flow_id": token,
        "version": 3,
    }


def test_response_engine_conversation_flow_omits_null_version():
    token = ids.encode_conversation_flow_id(uuid.uuid4())
    p = _profile(
        {
            "compat_response_engine": {
                "type": "conversation-flow",
                "conversation_flow_id": token,
                "version": None,
            }
        }
    )
    eng = agent_bridge._response_engine(p)
    assert eng == {"type": "conversation-flow", "conversation_flow_id": token}
    assert "version" not in eng


def test_response_engine_ignores_none_draft_config():
    p = SimpleNamespace(id=uuid.uuid4(), draft_config=None)
    assert agent_bridge._response_engine(p)["type"] == "retell-llm"
