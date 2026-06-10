"""Pure unit tests for schemas/schedule — create/update validation + response render.

Covers the quiet-hours window contract (spec §4.1/§6.3): the schema layer 422s a
window that never intersects [09:00, 21:00) before any DB or tz work happens.
No DB.
"""

import uuid
from datetime import UTC, date, datetime, time

import pytest
from pydantic import ValidationError

from usan_api.schedule_windows import DAY_NAMES


def test_create_defaults():
    from usan_api.schemas.schedule import CreateScheduleRequest

    req = CreateScheduleRequest(
        elder_id=uuid.uuid4(),
        window_start_local="09:00",
        window_end_local="17:00",
    )
    assert req.days_of_week == list(DAY_NAMES)
    assert req.enabled is True
    assert req.dynamic_vars == {}
    assert req.profile_override is None
    # "HH:MM" strings parse to datetime.time
    assert req.window_start_local == time(9, 0)
    assert req.window_end_local == time(17, 0)
    # the bitmask property mirrors schedule_windows.days_to_mask (all 7 days = 127)
    assert req.days_mask == 127


def test_create_rejects_empty_days():
    from usan_api.schemas.schedule import CreateScheduleRequest

    with pytest.raises(ValidationError, match="empty"):
        CreateScheduleRequest(
            elder_id=uuid.uuid4(),
            window_start_local="09:00",
            window_end_local="17:00",
            days_of_week=[],
        )


def test_create_rejects_unknown_day():
    from usan_api.schemas.schedule import CreateScheduleRequest

    with pytest.raises(ValidationError, match="monday"):
        CreateScheduleRequest(
            elder_id=uuid.uuid4(),
            window_start_local="09:00",
            window_end_local="17:00",
            days_of_week=["monday"],
        )


def test_create_rejects_duplicate_days():
    from usan_api.schemas.schedule import CreateScheduleRequest

    with pytest.raises(ValidationError, match="duplicate"):
        CreateScheduleRequest(
            elder_id=uuid.uuid4(),
            window_start_local="09:00",
            window_end_local="17:00",
            days_of_week=["mon", "mon"],
        )


def test_create_rejects_start_not_before_end():
    from usan_api.schemas.schedule import CreateScheduleRequest

    with pytest.raises(ValidationError, match="before"):
        CreateScheduleRequest(
            elder_id=uuid.uuid4(),
            window_start_local="17:00",
            window_end_local="09:00",
        )
    with pytest.raises(ValidationError, match="before"):
        CreateScheduleRequest(
            elder_id=uuid.uuid4(),
            window_start_local="10:00",
            window_end_local="10:00",
        )


def test_create_rejects_window_outside_quiet_hours():
    from usan_api.schemas.schedule import CreateScheduleRequest

    with pytest.raises(ValidationError, match="quiet hours"):
        CreateScheduleRequest(
            elder_id=uuid.uuid4(),
            window_start_local="21:30",
            window_end_local="22:30",
        )


def test_create_caps_dynamic_vars_at_8kb():
    from usan_api.schemas import call as call_schemas
    from usan_api.schemas import schedule as schedule_schemas

    # The cap constant is IMPORTED from schemas.call, not redefined: a literal
    # 8192 in another module would be a distinct int object, so identity pins it.
    assert schedule_schemas.MAX_DYNAMIC_VARS_BYTES is call_schemas.MAX_DYNAMIC_VARS_BYTES

    with pytest.raises(ValidationError, match="8192"):
        schedule_schemas.CreateScheduleRequest(
            elder_id=uuid.uuid4(),
            window_start_local="09:00",
            window_end_local="17:00",
            dynamic_vars={"note": "x" * (call_schemas.MAX_DYNAMIC_VARS_BYTES + 1)},
        )


def test_update_all_fields_optional():
    from usan_api.schemas.schedule import UpdateScheduleRequest

    req = UpdateScheduleRequest()
    assert req.enabled is None
    assert req.window_start_local is None
    assert req.window_end_local is None
    assert req.days_of_week is None
    assert req.dynamic_vars is None
    assert req.profile_override is None

    # any single non-window field may travel alone
    assert UpdateScheduleRequest(enabled=False).enabled is False
    assert UpdateScheduleRequest(days_of_week=["sun", "mon"]).days_of_week == ["mon", "sun"]


def test_update_rejects_half_window():
    from usan_api.schemas.schedule import UpdateScheduleRequest

    # window fields travel together on PATCH
    with pytest.raises(ValidationError, match="together"):
        UpdateScheduleRequest(window_start_local="10:00")
    with pytest.raises(ValidationError, match="together"):
        UpdateScheduleRequest(window_end_local="16:00")
    # when both present, the create-time rules apply
    with pytest.raises(ValidationError, match="before"):
        UpdateScheduleRequest(window_start_local="16:00", window_end_local="10:00")
    with pytest.raises(ValidationError, match="quiet hours"):
        UpdateScheduleRequest(window_start_local="21:30", window_end_local="22:30")


def test_schedule_response_from_model_renders_day_list():
    from usan_api.schemas.schedule import ScheduleResponse

    class _Row:
        id = uuid.uuid4()
        elder_id = uuid.uuid4()
        enabled = True
        window_start_local = time(9, 0)
        window_end_local = time(17, 0)
        days_of_week = 65  # 0b1000001 = mon + sun
        dynamic_vars = {"memory_care": "true"}
        profile_override = None
        next_run_at = datetime(2026, 6, 14, 13, 0, tzinfo=UTC)
        last_materialized_date = date(2026, 6, 7)
        last_result = "completed"
        last_result_at = datetime(2026, 6, 7, 13, 30, tzinfo=UTC)
        created_at = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        updated_at = datetime(2026, 6, 7, 13, 30, tzinfo=UTC)

    row = _Row()
    resp = ScheduleResponse.from_model(row)
    assert resp.id == row.id
    assert resp.elder_id == row.elder_id
    assert resp.days_of_week == ["mon", "sun"]
    assert resp.next_run_at == row.next_run_at
    assert resp.last_materialized_date == date(2026, 6, 7)
    assert resp.last_result == "completed"
    assert resp.last_result_at == row.last_result_at


def test_max_schedules_limit_constant():
    from usan_api.schemas.schedule import MAX_SCHEDULES_LIMIT

    assert MAX_SCHEDULES_LIMIT == 500
