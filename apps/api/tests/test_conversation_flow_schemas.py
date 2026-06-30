from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usan_api.compat.schemas.conversation_flow import (
    CreateConversationFlowRequest,
    UpdateConversationFlowRequest,
    serialize_flow,
)
from usan_api.db.models import ConversationFlow

_MODEL = {"type": "cascading", "model": "gpt-4.1"}


def test_create_request_requires_three_fields() -> None:
    with pytest.raises(ValidationError):
        CreateConversationFlowRequest(model_choice=_MODEL, nodes=[])  # missing start_speaker
    with pytest.raises(ValidationError):
        CreateConversationFlowRequest(start_speaker="agent", nodes=[])  # missing model_choice


def test_create_request_captures_extras() -> None:
    body = CreateConversationFlowRequest(
        start_speaker="agent",
        model_choice=_MODEL,
        nodes=[{"id": "n1", "type": "conversation"}],
        global_prompt="hi",
        tools=[{"type": "end_call"}],
    )
    dumped = body.model_dump()
    assert dumped["start_speaker"] == "agent"
    assert dumped["global_prompt"] == "hi"
    assert dumped["tools"] == [{"type": "end_call"}]
    assert dumped["nodes"] == [{"id": "n1", "type": "conversation"}]


def test_update_request_is_opaque_partial() -> None:
    body = UpdateConversationFlowRequest(global_prompt="b")
    assert body.model_dump() == {"global_prompt": "b"}


def test_serialize_flow_echoes_config_and_server_fields() -> None:
    fid = uuid.uuid4()
    row = ConversationFlow(config={"start_speaker": "agent", "global_prompt": "hi"}, version=2)
    row.id = fid
    row.updated_at = datetime(2026, 6, 30, tzinfo=UTC)
    out = serialize_flow(row)
    assert out["conversation_flow_id"] == "conversation_flow_" + fid.hex
    assert out["version"] == 2
    assert out["start_speaker"] == "agent"
    assert out["global_prompt"] == "hi"
    expected_ms = int(datetime(2026, 6, 30, tzinfo=UTC).timestamp() * 1000)
    assert out["last_modification_timestamp"] == expected_ms
