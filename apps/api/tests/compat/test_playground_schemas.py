from __future__ import annotations

import pytest
from pydantic import ValidationError

from usan_api.compat.schemas.playground import (
    PlaygroundCompletionRequest,
    PlaygroundCompletionResponse,
    PlaygroundMessageOut,
)


def test_request_requires_non_empty_messages() -> None:
    with pytest.raises(ValidationError):
        PlaygroundCompletionRequest(messages=[])


def test_request_accepts_advanced_fields_and_extra_keys() -> None:
    req = PlaygroundCompletionRequest.model_validate(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "dynamic_variables": {"name": "Jo"},
            "tool_mocks": [{"tool_name": "x", "output": "y", "input_match_rule": "any"}],
            "current_state": "greeting",
            "current_node_id": "node_1",
            "component_id": "comp_1",
            "some_future_key": True,
        }
    )
    assert req.messages[0].role == "user"
    assert req.messages[0].content == "hi"
    assert req.dynamic_variables == {"name": "Jo"}


def test_request_tolerates_content_less_variant() -> None:
    # a tool-call ChatMessageInput variant carries no `content` — must not error
    req = PlaygroundCompletionRequest.model_validate(
        {"messages": [{"role": "tool_call_invocation", "tool_call_id": "t1"}]}
    )
    assert req.messages[0].content is None


def test_response_exclude_none_omits_unproduced_fields() -> None:
    resp = PlaygroundCompletionResponse(
        messages=[PlaygroundMessageOut(message_id="m1", content="hello", created_timestamp=1)]
    )
    dumped = resp.model_dump(exclude_none=True)
    assert dumped == {
        "messages": [
            {"message_id": "m1", "role": "agent", "content": "hello", "created_timestamp": 1}
        ]
    }
    for k in (
        "current_state",
        "current_node_id",
        "dynamic_variables",
        "call_ended",
        "knowledge_base_retrieved_contents",
    ):
        assert k not in dumped
