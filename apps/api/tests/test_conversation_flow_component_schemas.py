from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usan_api.compat.schemas.conversation_flow_component import (
    CreateConversationFlowComponentRequest,
    UpdateConversationFlowComponentRequest,
    serialize_component,
)
from usan_api.db.models import ConversationFlowComponent


def test_create_request_requires_name_and_nodes() -> None:
    with pytest.raises(ValidationError):
        CreateConversationFlowComponentRequest(nodes=[])  # missing name
    with pytest.raises(ValidationError):
        CreateConversationFlowComponentRequest(name="Collector")  # missing nodes


def test_create_request_captures_extras() -> None:
    body = CreateConversationFlowComponentRequest(
        name="Collector",
        nodes=[{"id": "n1", "type": "conversation"}],
        flex_mode=True,
        tools=[{"type": "end_call"}],
    )
    dumped = body.model_dump()
    assert dumped["name"] == "Collector"
    assert dumped["flex_mode"] is True
    assert dumped["tools"] == [{"type": "end_call"}]
    assert dumped["nodes"] == [{"id": "n1", "type": "conversation"}]


def test_update_request_is_opaque_partial() -> None:
    body = UpdateConversationFlowComponentRequest(flex_mode=False)
    assert body.model_dump() == {"flex_mode": False}


def test_serialize_component_echoes_config_and_server_fields() -> None:
    cid = uuid.uuid4()
    row = ConversationFlowComponent(config={"name": "Collector", "flex_mode": True})
    row.id = cid
    row.updated_at = datetime(2026, 6, 30, tzinfo=UTC)
    out = serialize_component(row)
    assert out["conversation_flow_component_id"] == "conversation_flow_component_" + cid.hex
    assert out["name"] == "Collector"
    assert out["flex_mode"] is True
    assert "version" not in out  # components have no version field
    expected_ms = int(datetime(2026, 6, 30, tzinfo=UTC).timestamp() * 1000)
    assert out["user_modified_timestamp"] == expected_ms
