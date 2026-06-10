"""Admin read-model summaries for the Phase-3 tool tables."""

import uuid
from datetime import UTC, datetime


def test_followup_flag_summary_from_attributes():
    from usan_api.schemas.admin_tools import FollowupFlagSummary

    class _Row:
        id = 5
        call_id = uuid.uuid4()
        elder_id = uuid.uuid4()
        # C3 elder identity: masked_phone is REQUIRED — computed by the router
        # helpers via mask_phone, never read off an ORM row, so stubs carry it.
        elder_name = None
        masked_phone = "***4567"
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
    assert s.elder_name is None
    assert s.masked_phone == "***4567"


def test_callback_request_summary_from_attributes():
    from usan_api.schemas.admin_tools import CallbackRequestSummary

    class _Row:
        id = 7
        call_id = uuid.uuid4()
        elder_id = uuid.uuid4()
        # C3 elder identity (masked_phone required; see flag stub note above).
        elder_name = None
        masked_phone = "***4567"
        requested_time_text = "tomorrow afternoon"
        requested_at = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
        notes = "prefers afternoons"
        status = "open"
        created_at = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)

    row = _Row()
    summary = CallbackRequestSummary.model_validate(row)
    assert summary.id == 7
    assert summary.call_id == row.call_id
    assert summary.elder_id == row.elder_id
    assert summary.elder_name is None
    assert summary.masked_phone == "***4567"
    assert summary.requested_time_text == "tomorrow afternoon"
    assert summary.requested_at == row.requested_at
    assert summary.notes == "prefers afternoons"
    assert summary.status == "open"
    assert summary.created_at == row.created_at


def test_callback_request_summary_allows_null_requested_at_and_notes():
    from usan_api.schemas.admin_tools import CallbackRequestSummary

    class _Row:
        id = 8
        call_id = uuid.uuid4()
        elder_id = uuid.uuid4()
        # C3 elder identity (masked_phone required; see flag stub note above).
        elder_name = None
        masked_phone = "***4567"
        requested_time_text = "soon"
        requested_at = None
        notes = None
        status = "open"
        created_at = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)

    summary = CallbackRequestSummary.model_validate(_Row())
    assert summary.requested_at is None
    assert summary.notes is None
    assert summary.elder_name is None
    assert summary.masked_phone == "***4567"
