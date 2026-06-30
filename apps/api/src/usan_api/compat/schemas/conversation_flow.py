"""RetellAI-compat conversation-flow request/response schemas + serializer (Phase 6a).

The flow body is captured opaquely (extra='allow'): only the 3 oracle-required create fields
are presence-checked; nodes/tools/components/mcps and every other field ride through unvalidated
and are persisted/echoed verbatim (persisted-not-honored — semantic validation is the runtime's
job). serialize_flow echoes the stored body + the 3 server-generated fields.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from usan_api.compat import ids
from usan_api.db.models import ConversationFlow


class CreateConversationFlowRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    start_speaker: str
    model_choice: dict[str, Any]
    nodes: list[Any]


class UpdateConversationFlowRequest(BaseModel):
    """Oracle ConversationFlow: every field optional. Opaque — any subset of top-level fields
    is accepted and shallow-merged over the stored config by the router."""

    model_config = ConfigDict(extra="allow")


def serialize_flow(row: ConversationFlow) -> dict[str, Any]:
    data: dict[str, Any] = dict(row.config)
    data["conversation_flow_id"] = ids.encode_conversation_flow_id(row.id)
    data["version"] = row.version
    data["last_modification_timestamp"] = int(row.updated_at.timestamp() * 1000)
    return data
