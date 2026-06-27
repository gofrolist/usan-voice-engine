"""Phase 4c-1: chat-agent request/response schema shapes."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from usan_api.compat.schemas.chat_agents import (
    ChatAgentCreateRequest,
    ChatAgentResponse,
    ChatAgentUpdateRequest,
)


def test_create_requires_response_engine() -> None:
    with pytest.raises(ValidationError):
        ChatAgentCreateRequest(agent_name="x")  # type: ignore[call-arg]
    ok = ChatAgentCreateRequest(response_engine={"type": "retell-llm", "llm_id": "llm_abc"})
    assert ok.response_engine.llm_id == "llm_abc"


def test_update_all_optional_and_echoes_extras() -> None:
    body = ChatAgentUpdateRequest(auto_close_message="bye")  # extra='allow'
    assert body.response_engine is None
    assert body.model_dump()["auto_close_message"] == "bye"


def test_response_echoes_extra_fields() -> None:
    resp = ChatAgentResponse(
        agent_id="agent_x",
        response_engine={"type": "retell-llm", "llm_id": "llm_x"},
        version=1,
        is_published=True,
        last_modification_timestamp=123,
        auto_close_message="bye",  # extra='allow'
    )
    dumped = resp.model_dump(exclude_none=True)
    assert dumped["auto_close_message"] == "bye"
    assert "agent_name" not in dumped  # None omitted
