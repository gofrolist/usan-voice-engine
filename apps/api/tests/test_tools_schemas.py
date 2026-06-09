import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usan_api.schemas.tools import CallbackScheduledResponse, ScheduleCallbackRequest

_CID = uuid.uuid4()


def test_schedule_callback_request_minimal():
    req = ScheduleCallbackRequest(call_id=_CID, requested_time_text="tomorrow at 3")
    assert req.requested_time_text == "tomorrow at 3"
    assert req.requested_at is None
    assert req.notes is None


def test_schedule_callback_request_full():
    when = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
    req = ScheduleCallbackRequest(
        call_id=_CID,
        requested_time_text="tomorrow at 3",
        requested_at=when,
        notes="prefers afternoons",
    )
    assert req.requested_at == when
    assert req.notes == "prefers afternoons"


def test_schedule_callback_request_rejects_empty_time_text():
    with pytest.raises(ValidationError):
        ScheduleCallbackRequest(call_id=_CID, requested_time_text="")


def test_schedule_callback_request_caps_time_text_length():
    with pytest.raises(ValidationError):
        ScheduleCallbackRequest(call_id=_CID, requested_time_text="x" * 201)


def test_schedule_callback_request_caps_notes_length():
    with pytest.raises(ValidationError):
        ScheduleCallbackRequest(call_id=_CID, requested_time_text="soon", notes="y" * 2001)


def test_callback_scheduled_response_shape():
    resp = CallbackScheduledResponse(id=42)
    assert resp.id == 42
