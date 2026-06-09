"""Admin read-model summaries for the Phase-3 tool tables."""

import uuid
from datetime import UTC, datetime


def test_followup_flag_summary_from_attributes():
    from usan_api.schemas.admin_tools import FollowupFlagSummary

    class _Row:
        id = 5
        call_id = uuid.uuid4()
        elder_id = uuid.uuid4()
        severity = "urgent"
        category = "medical"
        reason = "chest pain"
        status = "open"
        created_at = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)

    s = FollowupFlagSummary.model_validate(_Row())
    assert s.id == 5
    assert s.severity == "urgent"
    assert s.reason == "chest pain"  # admin read exposes PHI reason (audited)
    assert s.status == "open"
