"""The three batch-calling ORM models mirror the migration 0012 schema."""

from sqlalchemy import SmallInteger
from sqlalchemy.dialects.postgresql import JSONB

from usan_api.db.models import CallBatch, CallBatchTarget, CallSchedule


def test_call_schedule_columns_and_fk():
    assert CallSchedule.__tablename__ == "call_schedules"
    cols = CallSchedule.__table__.columns
    assert {
        "id",
        "elder_id",
        "enabled",
        "window_start_local",
        "window_end_local",
        "days_of_week",
        "dynamic_vars",
        "profile_override",
        "next_run_at",
        "last_materialized_date",
        "last_result",
        "last_result_at",
        "created_at",
        "updated_at",
    } <= set(cols.keys())
    # Read FK rules without mutating the shared Table metadata (no .pop()).
    # CASCADE: a schedule is meaningless without its elder; one schedule per elder.
    assert next(iter(cols["elder_id"].foreign_keys)).ondelete == "CASCADE"
    assert cols["elder_id"].unique is True
    assert next(iter(cols["profile_override"].foreign_keys)).ondelete == "SET NULL"
    assert "127" in str(cols["days_of_week"].server_default.arg)
    assert not cols["next_run_at"].nullable
    assert cols["updated_at"].onupdate is not None


def test_call_batch_columns_and_defaults():
    assert CallBatch.__tablename__ == "call_batches"
    cols = CallBatch.__table__.columns
    assert not cols["payload_digest"].nullable
    assert cols["idempotency_key"].unique is True
    assert "'scheduled'" in str(cols["status"].server_default.arg)
    assert cols["window_start_local"].nullable
    assert cols["max_concurrency"].nullable
    assert isinstance(cols["max_concurrency"].type, SmallInteger)


def test_call_batch_target_columns_and_fks():
    assert CallBatchTarget.__tablename__ == "call_batch_targets"
    cols = CallBatchTarget.__table__.columns
    assert next(iter(cols["batch_id"].foreign_keys)).ondelete == "CASCADE"
    # SET NULL (not CASCADE): a deleted elder must not silently shrink the batch;
    # the poller marks the orphan target skipped/elder_deleted instead.
    assert next(iter(cols["elder_id"].foreign_keys)).ondelete == "SET NULL"
    assert cols["elder_id"].nullable
    assert next(iter(cols["call_id"].foreign_keys)).ondelete == "SET NULL"
    assert "'pending'" in str(cols["status"].server_default.arg)
    assert isinstance(cols["dynamic_vars"].type, JSONB)
    assert not cols["dynamic_vars"].nullable
    assert not cols["target_index"].nullable
