"""RetellAI-compat conversation-flow-component request/response schemas + serializer (Phase 6b).

The component body is captured opaquely (extra='allow'): only the 2 oracle-required create fields
(name, nodes) are presence-checked; every other field rides through unvalidated and is
persisted/echoed verbatim (persisted-not-honored — semantic validation is the runtime's job).
serialize_component echoes the stored body + the 2 server-generated fields. No version field:
the oracle ConversationFlowComponentResponse has none.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from usan_api.compat import ids
from usan_api.db.models import ConversationFlowComponent


class CreateConversationFlowComponentRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    nodes: list[Any]


class UpdateConversationFlowComponentRequest(BaseModel):
    """Oracle ConversationFlowComponent: every field optional. Opaque — any subset of top-level
    fields is accepted and shallow-merged over the stored config by the router."""

    model_config = ConfigDict(extra="allow")


def serialize_component(row: ConversationFlowComponent) -> dict[str, Any]:
    data: dict[str, Any] = dict(row.config)
    data["conversation_flow_component_id"] = ids.encode_conversation_flow_component_id(row.id)
    data["user_modified_timestamp"] = int(row.updated_at.timestamp() * 1000)
    return data
