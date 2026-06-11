"""Migration 0015 adds the custom_variables catalog table.

Runs against the testcontainers Postgres that conftest's `database_url` already
migrated to head. We introspect the live catalog so the tests fail before 0015
exists (table absent) and pass once it lands.

Helpers cloned verbatim from test_ops_queue_migration.py (`_columns` returning
{name: (data_type, is_nullable, column_default)}, `_indexes`,
`_check_constraints`, `_execute`, plus its API_DIR/env-dict subprocess pattern).
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
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


def test_custom_variables_table_shape(async_database_url: str) -> None:
    cols = asyncio.run(_columns(async_database_url, "custom_variables"))

    data_type, nullable, default = cols["id"]
    assert data_type == "uuid"
    assert default is not None
    assert "gen_random_uuid" in default

    data_type, nullable, default = cols["name"]
    assert (data_type, nullable) == ("text", "NO")

    data_type, nullable, default = cols["description"]
    assert (data_type, nullable) == ("text", "NO")
    assert default is not None
    assert "''" in default

    data_type, nullable, default = cols["example"]
    assert (data_type, nullable) == ("text", "NO")
    assert default is not None
    assert "''" in default

    data_type, nullable, default = cols["phi"]
    assert (data_type, nullable) == ("boolean", "NO")
    assert default is not None
    assert "false" in default

    for col in ("created_at", "updated_at"):
        data_type, nullable, default = cols[col]
        assert (data_type, nullable) == ("timestamp with time zone", "NO")
        assert default is not None
        assert "now()" in default

    checks = asyncio.run(_check_constraints(async_database_url, "custom_variables"))
    assert "ck_custom_variables_name_slug" in checks

    idx = asyncio.run(_indexes(async_database_url, "custom_variables"))
    assert "custom_variables_name_key" in idx


def test_slug_check_enforced(async_database_url: str) -> None:
    for bad_name in ("Bad", "9starts_with_digit", "has space", "has-dash", "a" * 65):
        with pytest.raises((IntegrityError, DBAPIError)):
            # engine.begin() rolls the failed transaction back on exit.
            asyncio.run(
                _execute(
                    async_database_url,
                    "INSERT INTO custom_variables (name) VALUES (:name)",
                    {"name": bad_name},
                )
            )

    # A conforming slug inserts fine. The insert bypasses the `client` fixture's
    # teardown TRUNCATE, so the seeded row is deleted explicitly at the end.
    asyncio.run(
        _execute(
            async_database_url,
            "INSERT INTO custom_variables (name) VALUES (:name)",
            {"name": "ok_name_1"},
        )
    )
    asyncio.run(
        _execute(
            async_database_url,
            "DELETE FROM custom_variables WHERE name = :name",
            {"name": "ok_name_1"},
        )
    )


def test_unique_name_enforced(async_database_url: str) -> None:
    asyncio.run(
        _execute(
            async_database_url,
            "INSERT INTO custom_variables (name) VALUES (:name)",
            {"name": "pet_name"},
        )
    )
    try:
        with pytest.raises(IntegrityError):
            asyncio.run(
                _execute(
                    async_database_url,
                    "INSERT INTO custom_variables (name) VALUES (:name)",
                    {"name": "pet_name"},
                )
            )
    finally:
        asyncio.run(
            _execute(
                async_database_url,
                "DELETE FROM custom_variables WHERE name = :name",
                {"name": "pet_name"},
            )
        )


def test_downgrade_upgrade_roundtrip(database_url: str, async_database_url: str) -> None:
    # Runs last in this module; leaves the session DB clean at head. The
    # subprocess roundtrip pattern from test_ops_queue_migration.py — conftest
    # migrates to head before tests run.
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
        [sys.executable, "-m", "alembic", "downgrade", "0014"],
        cwd=API_DIR,
        env=env,
        check=True,
    )
    try:
        cols = asyncio.run(_columns(async_database_url, "custom_variables"))
        assert cols == {}
    finally:
        # A mid-test failure must not strand the shared session DB at 0014 —
        # every other module in the run assumes head.
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=API_DIR,
            env=env,
            check=True,
        )

    cols = asyncio.run(_columns(async_database_url, "custom_variables"))
    assert "id" in cols
    checks = asyncio.run(_check_constraints(async_database_url, "custom_variables"))
    assert "ck_custom_variables_name_slug" in checks
