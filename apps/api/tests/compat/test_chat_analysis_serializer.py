"""Phase 4c-2: serialize_chat emits chat_analysis only when a record is passed (pass-through)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from usan_api.compat.chat_serializer import serialize_chat
from usan_api.db.base import ChatStatus
from usan_api.db.models import ChatAnalysisRecord, ChatSession


def _session() -> ChatSession:
    s = ChatSession(
        id=uuid.uuid4(),
        agent_profile_id=uuid.uuid4(),
        agent_version=1,
        status=ChatStatus.ENDED,
        chat_type="api_chat",
        dynamic_vars={},
    )
    s.started_at = datetime(2026, 1, 1, tzinfo=UTC)
    s.ended_at = datetime(2026, 1, 1, tzinfo=UTC)
    return s


def test_no_analysis_omits_field():
    out = serialize_chat(_session(), [], include_transcript=False).model_dump(exclude_none=True)
    assert "chat_analysis" not in out


def test_analysis_passed_through():
    rec = ChatAnalysisRecord(
        chat_session_id=uuid.uuid4(),
        chat_summary="A warm check-in.",
        user_sentiment="Positive",
        chat_successful=True,
        custom_analysis_data=None,
    )
    out = serialize_chat(_session(), [], include_transcript=False, analysis=rec).model_dump(
        exclude_none=True
    )
    assert out["chat_analysis"]["chat_summary"] == "A warm check-in."
    assert out["chat_analysis"]["user_sentiment"] == "Positive"
    assert out["chat_analysis"]["chat_successful"] is True
    assert "custom_analysis_data" not in out["chat_analysis"]  # None -> omitted
