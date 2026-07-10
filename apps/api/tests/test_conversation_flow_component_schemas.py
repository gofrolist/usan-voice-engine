from __future__ import annotations

import uuid
from datetime import UTC, datetime

from usan_api.compat.schemas.conversation_flow_component import serialize_component
from usan_api.db.models import ConversationFlowComponent


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
