"""Pure unit tests for schemas/batch — caps, duplicate-contact 422, naive-UTC coercion,
quiet-hours window contract, and the canonical sha256 payload digest (spec §4.2).

The digest contract is load-bearing for replay: same idempotency_key + same digest
-> 200 with the existing batch; same key + different digest -> 409. So the digest
must be insensitive to dict key order (sorted-key JSON) but sensitive to target
order and every batch-defining field — and must exclude the idempotency_key itself.
No DB.
"""

import uuid
from datetime import UTC, datetime, time, timedelta, timezone

import pytest
from pydantic import ValidationError


def _valid_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "june-wellness",
        "targets": [{"contact_id": str(uuid.uuid4())}],
    }
    return {**base, **overrides}


def test_target_caps_and_defaults():
    from usan_api.schemas import call as call_schemas
    from usan_api.schemas.batch import BatchTargetIn

    target = BatchTargetIn(contact_id=uuid.uuid4())
    assert target.dynamic_vars == {}
    assert target.profile_override is None

    # the 8 KB serialized cap is enforced per target (same contract as enqueue_call)
    with pytest.raises(ValidationError, match="8192"):
        BatchTargetIn(
            contact_id=uuid.uuid4(),
            dynamic_vars={"note": "x" * (call_schemas.MAX_DYNAMIC_VARS_BYTES + 1)},
        )


def test_create_rejects_zero_and_501_targets():
    from usan_api.schemas.batch import MAX_BATCH_TARGETS, CreateBatchRequest

    assert MAX_BATCH_TARGETS == 500

    with pytest.raises(ValidationError, match="at least 1"):
        CreateBatchRequest(**_valid_kwargs(targets=[]))

    too_many = [{"contact_id": str(uuid.uuid4())} for _ in range(MAX_BATCH_TARGETS + 1)]
    with pytest.raises(ValidationError, match="at most 500"):
        CreateBatchRequest(**_valid_kwargs(targets=too_many))

    # exactly 500 unique targets is accepted
    ok = CreateBatchRequest(**_valid_kwargs(targets=too_many[:MAX_BATCH_TARGETS]))
    assert len(ok.targets) == MAX_BATCH_TARGETS


def test_create_rejects_long_name():
    from usan_api.schemas.batch import MAX_BATCH_NAME_LENGTH, CreateBatchRequest

    assert MAX_BATCH_NAME_LENGTH == 200

    with pytest.raises(ValidationError, match="200"):
        CreateBatchRequest(**_valid_kwargs(name="x" * (MAX_BATCH_NAME_LENGTH + 1)))
    with pytest.raises(ValidationError, match="at least 1"):
        CreateBatchRequest(**_valid_kwargs(name=""))


def test_create_rejects_duplicate_contacts_with_index():
    from usan_api.schemas.batch import CreateBatchRequest

    dup = str(uuid.uuid4())
    targets = [
        {"contact_id": dup},
        {"contact_id": str(uuid.uuid4())},
        {"contact_id": dup},
    ]
    # the error names the offending (duplicate) target_index
    with pytest.raises(ValidationError, match="target_index 2"):
        CreateBatchRequest(**_valid_kwargs(targets=targets))


def test_create_naive_trigger_at_assumed_utc():
    from usan_api.schemas.batch import CreateBatchRequest

    # naive ISO string -> assumed UTC (precedent: ScheduleCallbackRequest.requested_at)
    req = CreateBatchRequest(**_valid_kwargs(trigger_at="2026-06-12T15:00:00"))
    assert req.trigger_at == datetime(2026, 6, 12, 15, 0, tzinfo=UTC)
    assert req.trigger_at is not None
    assert req.trigger_at.tzinfo == UTC

    # aware values pass through unchanged
    aware = datetime(2026, 6, 12, 11, 0, tzinfo=timezone(timedelta(hours=-4)))
    req2 = CreateBatchRequest(**_valid_kwargs(trigger_at=aware))
    assert req2.trigger_at == aware
    assert req2.trigger_at is not None
    assert req2.trigger_at.utcoffset() == timedelta(hours=-4)


def test_create_window_must_intersect_quiet_hours():
    from usan_api.schemas.batch import CreateBatchRequest

    with pytest.raises(ValidationError, match="quiet hours"):
        CreateBatchRequest(**_valid_kwargs(window={"start_local": "21:30", "end_local": "22:30"}))
    with pytest.raises(ValidationError, match="before"):
        CreateBatchRequest(**_valid_kwargs(window={"start_local": "17:00", "end_local": "09:00"}))
    # day names share the schedule contract: unknown rejected, canonical order out
    with pytest.raises(ValidationError, match="monday"):
        CreateBatchRequest(
            **_valid_kwargs(
                window={
                    "start_local": "09:00",
                    "end_local": "17:00",
                    "days_of_week": ["monday"],
                }
            )
        )
    ok = CreateBatchRequest(
        **_valid_kwargs(
            window={
                "start_local": "09:00",
                "end_local": "17:00",
                "days_of_week": ["sun", "mon"],
            }
        )
    )
    assert ok.window is not None
    assert ok.window.days_of_week == ["mon", "sun"]
    # days_of_week omitted = any day
    any_day = CreateBatchRequest(
        **_valid_kwargs(window={"start_local": "09:00", "end_local": "17:00"})
    )
    assert any_day.window is not None
    assert any_day.window.days_of_week is None


def test_payload_digest_stable_under_key_order():
    from usan_api.schemas.batch import CreateBatchRequest, payload_digest

    contact = str(uuid.uuid4())
    a = CreateBatchRequest(
        **_valid_kwargs(targets=[{"contact_id": contact, "dynamic_vars": {"a": "1", "b": "2"}}])
    )
    b = CreateBatchRequest(
        **_valid_kwargs(targets=[{"contact_id": contact, "dynamic_vars": {"b": "2", "a": "1"}}])
    )
    # pydantic preserves dict insertion order, so only sorted-key JSON makes these equal
    assert payload_digest(a) == payload_digest(b)

    digest = payload_digest(a)
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_payload_digest_sensitive_to_target_order_and_content():
    from usan_api.schemas.batch import CreateBatchRequest, payload_digest

    e1, e2 = str(uuid.uuid4()), str(uuid.uuid4())
    base_targets = [
        {"contact_id": e1, "dynamic_vars": {"k": "v"}},
        {"contact_id": e2},
    ]
    base = CreateBatchRequest(**_valid_kwargs(targets=base_targets))
    base_digest = payload_digest(base)

    # target ORDER is part of the payload (target_index assignment depends on it)
    swapped = CreateBatchRequest(**_valid_kwargs(targets=list(reversed(base_targets))))
    assert payload_digest(swapped) != base_digest

    # every batch-defining field perturbs the digest
    assert (
        payload_digest(CreateBatchRequest(**_valid_kwargs(targets=base_targets, max_concurrency=5)))
        != base_digest
    )
    assert (
        payload_digest(
            CreateBatchRequest(
                **_valid_kwargs(
                    targets=base_targets,
                    window={"start_local": "10:00", "end_local": "12:00"},
                )
            )
        )
        != base_digest
    )
    assert (
        payload_digest(CreateBatchRequest(**_valid_kwargs(targets=base_targets, name="other")))
        != base_digest
    )
    changed_var = [{"contact_id": e1, "dynamic_vars": {"k": "OTHER"}}, {"contact_id": e2}]
    assert payload_digest(CreateBatchRequest(**_valid_kwargs(targets=changed_var))) != base_digest

    # the idempotency_key itself is EXCLUDED: two requests differing only in key
    # must produce the same digest (the key selects the batch, the digest verifies it)
    k1 = CreateBatchRequest(**_valid_kwargs(targets=base_targets, idempotency_key="key-one"))
    k2 = CreateBatchRequest(**_valid_kwargs(targets=base_targets, idempotency_key="key-two"))
    assert payload_digest(k1) == payload_digest(k2) == base_digest


def test_batch_responses_from_model_render():
    from usan_api.schemas.batch import (
        BatchCounts,
        BatchDetailResponse,
        BatchSummaryResponse,
        BatchTargetResponse,
    )

    class _Batch:
        id = uuid.uuid4()
        name = "june-wellness"
        status = "running"
        trigger_at = datetime(2026, 6, 12, 15, 0, tzinfo=UTC)
        window_start_local = time(9, 0)
        window_end_local = time(17, 0)
        days_of_week = 65  # 0b1000001 = mon + sun
        max_concurrency = 5
        profile_override = None
        started_at = datetime(2026, 6, 12, 15, 0, tzinfo=UTC)
        completed_at = None
        cancelled_at = None
        created_at = datetime(2026, 6, 10, 9, 0, tzinfo=UTC)

    counts = BatchCounts(pending=1, materialized=1)
    summary = BatchSummaryResponse.from_model(_Batch(), counts)
    assert summary.id == _Batch.id
    assert summary.days_of_week == ["mon", "sun"]
    assert summary.counts.pending == 1
    assert summary.counts.done == 0

    class _NoWindow(_Batch):
        days_of_week = None
        window_start_local = None
        window_end_local = None

    no_window = BatchSummaryResponse.from_model(_NoWindow(), BatchCounts())
    assert no_window.days_of_week is None
    assert no_window.window_start_local is None

    class _Target:
        target_index = 0
        contact_id = uuid.uuid4()
        status = "done"
        skip_reason = None
        call_id = uuid.uuid4()
        final_status = "completed"
        materialized_at = datetime(2026, 6, 12, 15, 1, tzinfo=UTC)
        finalized_at = datetime(2026, 6, 12, 15, 20, tzinfo=UTC)

    target = BatchTargetResponse.from_model(_Target())
    assert target.target_index == 0
    assert target.final_status == "completed"

    detail = BatchDetailResponse(
        **summary.model_dump(),
        final_status_histogram={"completed": 1},
        targets=[target],
    )
    assert detail.final_status_histogram == {"completed": 1}
    assert detail.targets[0].call_id == _Target.call_id
