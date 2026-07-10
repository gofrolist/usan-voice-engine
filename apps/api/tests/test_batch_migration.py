"""Migration 0012 creates the batch/scheduled-calling tables with the right shape.

Runs against the testcontainers Postgres that conftest's `database_url` already
migrated to head. We introspect the live catalog so the tests fail before 0012
exists (tables/index absent) and pass once it lands.

Mirrors test_phase3_migration.py's async-introspection helpers, extended:
`_columns` also selects is_nullable + column_default (needed for the NOT NULL
and server-default assertions below), plus `_check_constraints` / `_indexdef`.
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def _columns(async_database_url: str, table: str) -> dict[str, tuple[str, str, str | None]]:
    """{column_name: (data_type, is_nullable, column_default)} for a table."""
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = :t AND table_schema = 'public' "
                    "ORDER BY ordinal_position"
                ),
                {"t": table},
            )
            return {r[0]: (r[1], r[2], r[3]) for r in rows}
    finally:
        await engine.dispose()


async def _indexes(async_database_url: str, table: str) -> set[str]:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = :t"),
                {"t": table},
            )
            return {r[0] for r in rows}
    finally:
        await engine.dispose()


async def _check_constraints(async_database_url: str, table: str) -> set[str]:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = CAST(:t AS regclass) AND contype = 'c'"
                ),
                {"t": table},
            )
            return {r[0] for r in rows}
    finally:
        await engine.dispose()


async def _indexdef(async_database_url: str, index: str) -> str:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :i"),
                {"i": index},
            )
            row = rows.first()
            return row[0] if row is not None else ""
    finally:
        await engine.dispose()


async def _delete_rules(async_database_url: str, table: str) -> dict[str, str]:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(
                text(
                    "SELECT ccu.table_name AS ref_table, rc.delete_rule "
                    "FROM information_schema.referential_constraints rc "
                    "JOIN information_schema.constraint_column_usage ccu "
                    "  ON ccu.constraint_name = rc.constraint_name "
                    "JOIN information_schema.table_constraints tc "
                    "  ON tc.constraint_name = rc.constraint_name "
                    "WHERE tc.table_name = :t AND tc.table_schema = 'public'"
                ),
                {"t": table},
            )
            return {r[0]: r[1] for r in rows}
    finally:
        await engine.dispose()


def test_call_schedules_table_shape(async_database_url: str) -> None:
    cols = asyncio.run(_columns(async_database_url, "call_schedules"))
    assert cols["id"][0] == "uuid"
    assert cols["contact_id"][0] == "uuid"
    assert cols["enabled"][0] == "boolean"
    assert cols["window_start_local"][0] == "time without time zone"
    assert cols["window_end_local"][0] == "time without time zone"
    assert cols["days_of_week"][0] == "smallint"
    assert cols["dynamic_vars"][0] == "jsonb"
    assert cols["profile_override"][0] == "uuid"
    assert cols["next_run_at"][0] == "timestamp with time zone"
    assert cols["last_materialized_date"][0] == "date"
    assert cols["last_result"][0] == "text"
    assert cols["last_result_at"][0] == "timestamp with time zone"
    assert cols["slot"][0] == "text"  # US5

    idx = asyncio.run(_indexes(async_database_url, "call_schedules"))
    assert "idx_call_schedules_due" in idx
    # US5 (migration 0022) relaxed UNIQUE(contact_id) to a composite
    # UNIQUE(contact_id, slot): one schedule per contact per morning|evening slot.
    # 0027 renames the table/columns/values but keeps internal index/constraint NAMES
    # historical (elder), so the live name stays uq_call_schedules_elder_slot.
    assert "call_schedules_elder_id_key" not in idx
    assert "uq_call_schedules_elder_slot" in idx
    due_def = asyncio.run(_indexdef(async_database_url, "idx_call_schedules_due"))
    assert "WHERE enabled" in due_def

    checks = asyncio.run(_check_constraints(async_database_url, "call_schedules"))
    assert "ck_call_schedules_window" in checks
    assert "ck_call_schedules_days" in checks
    assert "ck_call_schedules_result" in checks
    assert "ck_call_schedules_slot" in checks  # US5


def test_call_batches_table_shape(async_database_url: str) -> None:
    cols = asyncio.run(_columns(async_database_url, "call_batches"))
    assert cols["payload_digest"][0] == "text"
    assert cols["payload_digest"][1] == "NO"  # NOT NULL
    assert cols["status"][0] == "text"
    assert "scheduled" in (cols["status"][2] or "")  # DEFAULT 'scheduled'
    assert cols["trigger_at"][0] == "timestamp with time zone"
    assert cols["started_at"][0] == "timestamp with time zone"
    assert cols["completed_at"][0] == "timestamp with time zone"
    assert cols["cancelled_at"][0] == "timestamp with time zone"
    assert cols["max_concurrency"][0] == "smallint"

    checks = asyncio.run(_check_constraints(async_database_url, "call_batches"))
    assert "ck_call_batches_status" in checks
    assert "ck_call_batches_window" in checks
    assert "ck_call_batches_days" in checks
    assert "ck_call_batches_maxconc" in checks

    # Partial index on the scheduled status. status is TEXT, so Postgres renders
    # the predicate as (status = 'scheduled'::text) — don't assert a WHERE literal.
    due_def = asyncio.run(_indexdef(async_database_url, "idx_call_batches_due"))
    assert "WHERE" in due_def
    assert "scheduled" in due_def
    # Bounded working set: cancelled batches leave once drained (completed_at set).
    open_def = asyncio.run(_indexdef(async_database_url, "idx_call_batches_open"))
    assert "cancelled" in open_def
    assert "completed_at IS NULL" in open_def


def test_call_batch_targets_table_shape(async_database_url: str) -> None:
    cols = asyncio.run(_columns(async_database_url, "call_batch_targets"))
    assert cols["id"][0] == "bigint"
    assert cols["target_index"][0] == "integer"
    assert cols["contact_id"][0] == "uuid"
    assert cols["contact_id"][1] == "YES"  # nullable: SET NULL keeps the row
    assert cols["skip_reason"][0] == "text"
    assert cols["final_status"][0] == "text"
    assert cols["call_id"][0] == "uuid"
    assert cols["materialized_at"][0] == "timestamp with time zone"
    assert cols["finalized_at"][0] == "timestamp with time zone"

    idx = asyncio.run(_indexes(async_database_url, "call_batch_targets"))
    assert "uq_call_batch_targets_idx" in idx

    checks = asyncio.run(_check_constraints(async_database_url, "call_batch_targets"))
    assert "ck_call_batch_targets_status" in checks

    pending_def = asyncio.run(_indexdef(async_database_url, "idx_call_batch_targets_pending"))
    assert "WHERE" in pending_def
    assert "'pending'" in pending_def
    open_def = asyncio.run(_indexdef(async_database_url, "idx_call_batch_targets_open"))
    assert "WHERE" in open_def
    call_def = asyncio.run(_indexdef(async_database_url, "idx_call_batch_targets_call"))
    assert "WHERE (call_id IS NOT NULL)" in call_def


def test_fk_delete_rules(async_database_url: str) -> None:
    # CASCADE: a schedule is meaningless without its contact.
    rules = asyncio.run(_delete_rules(async_database_url, "call_schedules"))
    assert rules["contacts"] == "CASCADE"

    # Targets: CASCADE to the batch; SET NULL (not CASCADE) to contacts so a deleted
    # contact doesn't silently shrink the batch; SET NULL to calls/agent_profiles.
    rules = asyncio.run(_delete_rules(async_database_url, "call_batch_targets"))
    assert rules["call_batches"] == "CASCADE"
    assert rules["contacts"] == "SET NULL"
    assert rules["calls"] == "SET NULL"
    assert rules["agent_profiles"] == "SET NULL"


def test_idx_calls_in_flight_recency_keyed(async_database_url: str) -> None:
    idx = asyncio.run(_indexes(async_database_url, "calls"))
    assert "idx_calls_in_flight" in idx
    # Recency-bounded gate count (spec §3.4): keyed on updated_at under the static
    # status predicate. calls.status IS a PG enum, so the quoted status strings
    # appear in the rendered indexdef.
    indexdef = asyncio.run(_indexdef(async_database_url, "idx_calls_in_flight"))
    assert "(updated_at)" in indexdef
    assert "'dialing'" in indexdef
    assert "'ringing'" in indexdef
    assert "'in_progress'" in indexdef


# The downgrade→upgrade round-trip lives in test_migration_roundtrip.py, shared
# with the 0013/0014/0015 suites (one head→0011→head cycle instead of four).
