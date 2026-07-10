"""Migration 0013 adds the ops-queue status workflow + the global calls list index.

Runs against the testcontainers Postgres that conftest's `database_url` already
migrated to head. We introspect the live catalog so the tests fail before 0013
exists (columns/constraints/index absent) and pass once it lands.

Helpers copied verbatim from test_batch_migration.py (`_columns` returning
{name: (data_type, is_nullable, column_default)}, `_indexes`, `_indexdef`,
`_check_constraints`).
"""

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
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


async def _execute(async_database_url: str, sql: str, params: dict[str, Any]) -> None:
    """One statement in its own transaction on a throwaway engine.

    Each call gets its own `engine.begin()` block: a failed INSERT poisons the
    transaction (`InFailedSqlTransaction` on any follow-up statement), so seed,
    failing inserts, and cleanup must never share a connection.
    """
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params)
    finally:
        await engine.dispose()


def test_workflow_columns_both_tables(async_database_url: str) -> None:
    # NULL = never transitioned past 'open'; no backfill for existing rows.
    for table in ("follow_up_flags", "callback_requests"):
        cols = asyncio.run(_columns(async_database_url, table))
        assert cols["status_updated_at"] == ("timestamp with time zone", "YES", None)
        assert cols["status_updated_by"] == ("text", "YES", None)


def test_status_check_constraints_present_and_enforced(async_database_url: str) -> None:
    checks = asyncio.run(_check_constraints(async_database_url, "follow_up_flags"))
    assert "ck_follow_up_flags_status" in checks
    checks = asyncio.run(_check_constraints(async_database_url, "callback_requests"))
    assert "ck_callback_requests_status" in checks

    # Runtime enforcement (at HEAD: the table is `contacts`/`contact_id` after 0027). These
    # inserts bypass the `client` fixture's teardown TRUNCATE, so rows are deleted explicitly.
    contact_id = uuid.uuid4()
    call_id = uuid.uuid4()
    asyncio.run(
        _execute(
            async_database_url,
            "INSERT INTO contacts (id, name, phone_e164, timezone) "
            "VALUES (:id, 'Mig Test', '+19998880013', 'America/New_York')",
            {"id": contact_id},
        )
    )
    asyncio.run(
        _execute(
            async_database_url,
            "INSERT INTO calls (id, contact_id, direction, status) "
            "VALUES (:id, :contact_id, CAST('outbound' AS call_direction), "
            "CAST('queued' AS call_status))",
            {"id": call_id, "contact_id": contact_id},
        )
    )

    with pytest.raises(IntegrityError):
        # engine.begin() rolls the failed transaction back on exit.
        asyncio.run(
            _execute(
                async_database_url,
                "INSERT INTO follow_up_flags (call_id, contact_id, severity, category, status) "
                "VALUES (:call_id, :contact_id, 'routine', 'medical', 'bogus')",
                {"call_id": call_id, "contact_id": contact_id},
            )
        )

    with pytest.raises(IntegrityError):
        asyncio.run(
            _execute(
                async_database_url,
                "INSERT INTO callback_requests (call_id, contact_id, requested_time_text, status) "
                "VALUES (:call_id, :contact_id, 'x', 'bogus')",
                {"call_id": call_id, "contact_id": contact_id},
            )
        )

    asyncio.run(_execute(async_database_url, "DELETE FROM calls WHERE id = :id", {"id": call_id}))
    asyncio.run(
        _execute(async_database_url, "DELETE FROM contacts WHERE id = :id", {"id": contact_id})
    )


def test_idx_calls_created_shape(async_database_url: str) -> None:
    idx = asyncio.run(_indexes(async_database_url, "calls"))
    assert "idx_calls_created" in idx
    # Serves the global newest-first admin list; the per-elder slice keeps
    # idx_calls_elder.
    indexdef = asyncio.run(_indexdef(async_database_url, "idx_calls_created"))
    assert "(created_at DESC, id DESC)" in indexdef


# The downgrade→seed→upgrade round-trip (stray 'weird' statuses normalized to
# 'open' by 0013) lives in test_migration_roundtrip.py, shared with the
# 0012/0014/0015 suites (one head→0011→head cycle instead of four).
