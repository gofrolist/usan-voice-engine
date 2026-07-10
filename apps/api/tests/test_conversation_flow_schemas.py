from __future__ import annotations

import uuid
from datetime import UTC, datetime

from usan_api.compat.schemas.conversation_flow import serialize_flow
from usan_api.db.models import ConversationFlow


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
