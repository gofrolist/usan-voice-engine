"""Migration 0013 adds the ops-queue status workflow + the global calls list index.

Runs against the testcontainers Postgres that conftest's `database_url` already
migrated to head. We introspect the live catalog so the tests fail before 0013
exists (columns/constraints/index absent) and pass once it lands.

Helpers copied verbatim from test_batch_migration.py (`_columns` returning
{name: (data_type, is_nullable, column_default)}, `_indexes`, `_indexdef`,
`_check_constraints`, plus its API_DIR/env-dict subprocess pattern).
"""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

API_DIR = Path(__file__).resolve().parents[1]
TEST_SECRET = "a" * 32


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


async def _fetch_one(async_database_url: str, sql: str, params: dict[str, Any]) -> Any:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(text(sql), params)
            row = rows.first()
            return row[0] if row is not None else None
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

    # Runtime enforcement. These inserts bypass the `client` fixture's teardown
    # TRUNCATE, so the seeded rows are deleted explicitly at the end.
    elder_id = uuid.uuid4()
    call_id = uuid.uuid4()
    asyncio.run(
        _execute(
            async_database_url,
            "INSERT INTO elders (id, name, phone_e164, timezone) "
            "VALUES (:id, 'Mig Test', '+19998880013', 'America/New_York')",
            {"id": elder_id},
        )
    )
    asyncio.run(
        _execute(
            async_database_url,
            "INSERT INTO calls (id, elder_id, direction, status) "
            "VALUES (:id, :elder_id, CAST('outbound' AS call_direction), "
            "CAST('queued' AS call_status))",
            {"id": call_id, "elder_id": elder_id},
        )
    )

    with pytest.raises(IntegrityError):
        # engine.begin() rolls the failed transaction back on exit.
        asyncio.run(
            _execute(
                async_database_url,
                "INSERT INTO follow_up_flags (call_id, elder_id, severity, category, status) "
                "VALUES (:call_id, :elder_id, 'routine', 'medical', 'bogus')",
                {"call_id": call_id, "elder_id": elder_id},
            )
        )

    with pytest.raises(IntegrityError):
        asyncio.run(
            _execute(
                async_database_url,
                "INSERT INTO callback_requests (call_id, elder_id, requested_time_text, status) "
                "VALUES (:call_id, :elder_id, 'x', 'bogus')",
                {"call_id": call_id, "elder_id": elder_id},
            )
        )

    asyncio.run(_execute(async_database_url, "DELETE FROM calls WHERE id = :id", {"id": call_id}))
    asyncio.run(_execute(async_database_url, "DELETE FROM elders WHERE id = :id", {"id": elder_id}))


def test_idx_calls_created_shape(async_database_url: str) -> None:
    idx = asyncio.run(_indexes(async_database_url, "calls"))
    assert "idx_calls_created" in idx
    # Serves the global newest-first admin list; the per-elder slice keeps
    # idx_calls_elder.
    indexdef = asyncio.run(_indexdef(async_database_url, "idx_calls_created"))
    assert "(created_at DESC, id DESC)" in indexdef


def test_downgrade_seed_upgrade_normalizes_and_roundtrips(
    database_url: str, async_database_url: str
) -> None:
    # Runs last in this module; leaves the session DB clean at head. The
    # subprocess roundtrip pattern from test_batch_migration.py — conftest
    # migrates to head before tests run, so a head-only normalize test would be
    # vacuous: we must seed the 'weird' status while 0013 is downgraded.
    env = {
        **os.environ,
        "DATABASE_URL": database_url,
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": TEST_SECRET,
        "LIVEKIT_URL": "ws://livekit:7880",
        "JWT_SIGNING_KEY": "s" * 32,
        "OPERATOR_API_KEY": "o" * 32,
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0012"],
        cwd=API_DIR,
        env=env,
        check=True,
    )
    for table in ("follow_up_flags", "callback_requests"):
        cols = asyncio.run(_columns(async_database_url, table))
        assert "status_updated_at" not in cols
        assert "status_updated_by" not in cols
    checks = asyncio.run(_check_constraints(async_database_url, "follow_up_flags"))
    assert "ck_follow_up_flags_status" not in checks
    checks = asyncio.run(_check_constraints(async_database_url, "callback_requests"))
    assert "ck_callback_requests_status" not in checks
    assert "idx_calls_created" not in asyncio.run(_indexes(async_database_url, "calls"))

    # Seed a stray non-enum status — legal pre-CHECK.
    elder_id = uuid.uuid4()
    call_id = uuid.uuid4()
    asyncio.run(
        _execute(
            async_database_url,
            "INSERT INTO elders (id, name, phone_e164, timezone) "
            "VALUES (:id, 'Mig Roundtrip', '+19998880014', 'America/New_York')",
            {"id": elder_id},
        )
    )
    asyncio.run(
        _execute(
            async_database_url,
            "INSERT INTO calls (id, elder_id, direction, status) "
            "VALUES (:id, :elder_id, CAST('outbound' AS call_direction), "
            "CAST('queued' AS call_status))",
            {"id": call_id, "elder_id": elder_id},
        )
    )
    asyncio.run(
        _execute(
            async_database_url,
            "INSERT INTO follow_up_flags (call_id, elder_id, severity, category, status) "
            "VALUES (:call_id, :elder_id, 'routine', 'medical', 'weird')",
            {"call_id": call_id, "elder_id": elder_id},
        )
    )
    asyncio.run(
        _execute(
            async_database_url,
            "INSERT INTO callback_requests (call_id, elder_id, requested_time_text, status) "
            "VALUES (:call_id, :elder_id, 'x', 'weird')",
            {"call_id": call_id, "elder_id": elder_id},
        )
    )

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env=env,
        check=True,
    )

    # The defensive normalize rewrote the stray status before the CHECK landed.
    assert (
        asyncio.run(
            _fetch_one(
                async_database_url,
                "SELECT status FROM follow_up_flags WHERE call_id = :call_id",
                {"call_id": call_id},
            )
        )
        == "open"
    )
    assert (
        asyncio.run(
            _fetch_one(
                async_database_url,
                "SELECT status FROM callback_requests WHERE call_id = :call_id",
                {"call_id": call_id},
            )
        )
        == "open"
    )

    for table in ("follow_up_flags", "callback_requests"):
        cols = asyncio.run(_columns(async_database_url, table))
        assert "status_updated_at" in cols
        assert "status_updated_by" in cols
    checks = asyncio.run(_check_constraints(async_database_url, "follow_up_flags"))
    assert "ck_follow_up_flags_status" in checks
    checks = asyncio.run(_check_constraints(async_database_url, "callback_requests"))
    assert "ck_callback_requests_status" in checks
    assert "idx_calls_created" in asyncio.run(_indexes(async_database_url, "calls"))

    asyncio.run(
        _execute(
            async_database_url,
            "DELETE FROM follow_up_flags WHERE call_id = :call_id",
            {"call_id": call_id},
        )
    )
    asyncio.run(
        _execute(
            async_database_url,
            "DELETE FROM callback_requests WHERE call_id = :call_id",
            {"call_id": call_id},
        )
    )
    asyncio.run(_execute(async_database_url, "DELETE FROM calls WHERE id = :id", {"id": call_id}))
    asyncio.run(_execute(async_database_url, "DELETE FROM elders WHERE id = :id", {"id": elder_id}))
