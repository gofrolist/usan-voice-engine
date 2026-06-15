"""Migration 0014 adds the outbound-webhooks tables (spec 2026-06-10 §3.3).

`webhook_endpoints` (operator-registered destinations + circuit-breaker state)
and `webhook_deliveries` (the transactional outbox), plus the partial claim
index `idx_webhook_deliveries_due` and the operator list index
`idx_webhook_deliveries_endpoint`.

Runs against the testcontainers Postgres that conftest's `database_url` already
migrated to head. We introspect the live catalog so the tests fail before 0014
exists (tables absent) and pass once it lands.

Helpers copied verbatim from test_ops_queue_migration.py (`_columns` returning
{name: (data_type, is_nullable, column_default)}, `_indexes`, `_indexdef`,
`_check_constraints`, `_execute`, `_fetch_one`, plus its API_DIR/env-dict
subprocess pattern) and `_delete_rules` from test_batch_migration.py.
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


def test_webhook_endpoints_table_shape(async_database_url: str) -> None:
    cols = asyncio.run(_columns(async_database_url, "webhook_endpoints"))
    assert cols["id"][0] == "uuid"
    assert cols["url"][:2] == ("text", "NO")
    assert cols["description"][:2] == ("text", "YES")
    assert cols["enabled"][0] == "boolean"
    assert cols["enabled"][2] is not None
    assert "true" in cols["enabled"][2]
    # secret: server-generated 64-hex, returned once at create, never logged.
    assert cols["secret"][:2] == ("text", "NO")
    assert cols["events"][0] == "ARRAY"
    assert cols["consecutive_failures"][0] == "integer"
    assert cols["consecutive_failures"][2] is not None
    assert "0" in cols["consecutive_failures"][2]
    assert cols["disabled_reason"][:2] == ("text", "YES")
    assert cols["created_at"][:2] == ("timestamp with time zone", "NO")
    assert cols["updated_at"][:2] == ("timestamp with time zone", "NO")

    checks = asyncio.run(_check_constraints(async_database_url, "webhook_endpoints"))
    assert "ck_webhook_endpoints_events" in checks
    assert "ck_webhook_endpoints_disabled_reason" in checks
    assert "ck_webhook_endpoints_failures" in checks


def test_webhook_deliveries_table_shape(async_database_url: str) -> None:
    cols = asyncio.run(_columns(async_database_url, "webhook_deliveries"))
    assert cols["endpoint_id"][:2] == ("uuid", "NO")
    assert cols["event"][:2] == ("text", "NO")
    assert cols["payload"][:2] == ("jsonb", "NO")
    assert cols["status"][0] == "text"
    assert cols["status"][2] is not None
    assert "'pending'" in cols["status"][2]
    assert cols["attempts"][0] == "integer"
    assert cols["attempts"][2] is not None
    assert "0" in cols["attempts"][2]
    assert cols["next_attempt_at"][:2] == ("timestamp with time zone", "NO")
    assert cols["next_attempt_at"][2] is not None
    assert "now()" in cols["next_attempt_at"][2]
    assert cols["response_code"][:2] == ("integer", "YES")
    # last_error: exception TYPE NAME only, never str(exc).
    assert cols["last_error"][:2] == ("text", "YES")
    assert cols["delivered_at"][:2] == ("timestamp with time zone", "YES")

    checks = asyncio.run(_check_constraints(async_database_url, "webhook_deliveries"))
    assert "ck_webhook_deliveries_status" in checks
    assert "ck_webhook_deliveries_event" in checks
    assert "ck_webhook_deliveries_attempts" in checks


def test_due_index_is_partial_on_pending(async_database_url: str) -> None:
    idx = asyncio.run(_indexes(async_database_url, "webhook_deliveries"))
    assert "idx_webhook_deliveries_due" in idx
    indexdef = asyncio.run(_indexdef(async_database_url, "idx_webhook_deliveries_due"))
    # status is a TEXT column — do not assert an exact WHERE literal (Postgres
    # renders its own cast/quoting; the 0012 rendering lesson).
    assert "(next_attempt_at)" in indexdef
    assert "WHERE" in indexdef
    assert "pending" in indexdef


def test_endpoint_list_index_shape(async_database_url: str) -> None:
    idx = asyncio.run(_indexes(async_database_url, "webhook_deliveries"))
    assert "idx_webhook_deliveries_endpoint" in idx
    indexdef = asyncio.run(_indexdef(async_database_url, "idx_webhook_deliveries_endpoint"))
    assert "endpoint_id" in indexdef
    assert "created_at DESC" in indexdef
    assert "id DESC" in indexdef


def test_check_constraints_enforced(async_database_url: str) -> None:
    # Runtime enforcement. These inserts bypass the `client` fixture's teardown
    # TRUNCATE, so the seeded endpoint is deleted explicitly at the end (the
    # cascade removes its ping delivery).
    endpoint_id = uuid.uuid4()
    asyncio.run(
        _execute(
            async_database_url,
            "INSERT INTO webhook_endpoints (id, url, secret, events) "
            "VALUES (:id, 'https://hooks.example.com/x', :secret, "
            "CAST('{call.completed}' AS TEXT[]))",
            {"id": endpoint_id, "secret": "a" * 64},
        )
    )

    # Endpoint: empty subscription list.
    with pytest.raises((IntegrityError, DBAPIError)):
        asyncio.run(
            _execute(
                async_database_url,
                "INSERT INTO webhook_endpoints (url, secret, events) "
                "VALUES ('https://hooks.example.com/x', :secret, CAST('{}' AS TEXT[]))",
                {"secret": "a" * 64},
            )
        )

    # Endpoint: unknown event name in the list.
    with pytest.raises((IntegrityError, DBAPIError)):
        asyncio.run(
            _execute(
                async_database_url,
                "INSERT INTO webhook_endpoints (url, secret, events) "
                "VALUES ('https://hooks.example.com/x', :secret, "
                "CAST('{call.started,bogus}' AS TEXT[]))",
                {"secret": "a" * 64},
            )
        )

    # Endpoint: 'ping' is NOT subscribable (the load-bearing asymmetry — it is
    # only valid as a delivery event, below).
    with pytest.raises((IntegrityError, DBAPIError)):
        asyncio.run(
            _execute(
                async_database_url,
                "INSERT INTO webhook_endpoints (url, secret, events) "
                "VALUES ('https://hooks.example.com/x', :secret, CAST('{ping}' AS TEXT[]))",
                {"secret": "a" * 64},
            )
        )

    # Endpoint: disabled_reason outside the closed set.
    with pytest.raises((IntegrityError, DBAPIError)):
        asyncio.run(
            _execute(
                async_database_url,
                "INSERT INTO webhook_endpoints (url, secret, events, disabled_reason) "
                "VALUES ('https://hooks.example.com/x', :secret, "
                "CAST('{call.completed}' AS TEXT[]), 'manual')",
                {"secret": "a" * 64},
            )
        )

    # Delivery: status outside ('pending','delivered','failed').
    with pytest.raises((IntegrityError, DBAPIError)):
        asyncio.run(
            _execute(
                async_database_url,
                "INSERT INTO webhook_deliveries (endpoint_id, event, payload, status) "
                "VALUES (:eid, 'call.completed', CAST('{}' AS JSONB), 'sent')",
                {"eid": endpoint_id},
            )
        )

    # Delivery: event outside the closed enum.
    with pytest.raises((IntegrityError, DBAPIError)):
        asyncio.run(
            _execute(
                async_database_url,
                "INSERT INTO webhook_deliveries (endpoint_id, event, payload) "
                "VALUES (:eid, 'bogus', CAST('{}' AS JSONB))",
                {"eid": endpoint_id},
            )
        )

    # Delivery: negative attempts.
    with pytest.raises((IntegrityError, DBAPIError)):
        asyncio.run(
            _execute(
                async_database_url,
                "INSERT INTO webhook_deliveries (endpoint_id, event, payload, attempts) "
                "VALUES (:eid, 'call.completed', CAST('{}' AS JSONB), -1)",
                {"eid": endpoint_id},
            )
        )

    # Delivery: 'ping' IS a valid delivery event (the /test pipeline).
    asyncio.run(
        _execute(
            async_database_url,
            "INSERT INTO webhook_deliveries (endpoint_id, event, payload) "
            "VALUES (:eid, 'ping', CAST('{}' AS JSONB))",
            {"eid": endpoint_id},
        )
    )

    asyncio.run(
        _execute(
            async_database_url,
            "DELETE FROM webhook_endpoints WHERE id = :id",
            {"id": endpoint_id},
        )
    )


def test_fk_delete_rule_cascade(async_database_url: str) -> None:
    # Deleting an endpoint takes its delivery history with it.
    rules = asyncio.run(_delete_rules(async_database_url, "webhook_deliveries"))
    assert rules["webhook_endpoints"] == "CASCADE"


def test_downgrade_seed_upgrade_roundtrip(database_url: str, async_database_url: str) -> None:
    # Runs last in this module; always finishes at head (the shared session DB
    # must never be stranded mid-stack). Spec §10.1: downgrade → seed → upgrade —
    # upgrading 0014 over a *populated* pre-0014 database must work.
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
        [sys.executable, "-m", "alembic", "downgrade", "0013"],
        cwd=API_DIR,
        env=env,
        check=True,
    )
    call_id = uuid.uuid4()
    try:
        assert asyncio.run(_columns(async_database_url, "webhook_endpoints")) == {}
        assert asyncio.run(_columns(async_database_url, "webhook_deliveries")) == {}

        # Seed one minimal business row while downgraded: NOT NULL columns only,
        # nullable FKs (contact_id) left NULL.
        asyncio.run(
            _execute(
                async_database_url,
                "INSERT INTO calls (id, direction, status) "
                "VALUES (:id, CAST('outbound' AS call_direction), "
                "CAST('queued' AS call_status))",
                {"id": call_id},
            )
        )
    finally:
        # A mid-test failure must not strand the shared session DB at 0013 —
        # every other module in the run assumes head.
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=API_DIR,
            env=env,
            check=True,
        )

    assert "id" in asyncio.run(_columns(async_database_url, "webhook_endpoints"))
    assert "id" in asyncio.run(_columns(async_database_url, "webhook_deliveries"))
    idx = asyncio.run(_indexes(async_database_url, "webhook_deliveries"))
    assert "idx_webhook_deliveries_due" in idx

    assert (
        asyncio.run(
            _fetch_one(
                async_database_url,
                "SELECT count(*) FROM calls WHERE id = :id",
                {"id": call_id},
            )
        )
        == 1
    )

    asyncio.run(_execute(async_database_url, "DELETE FROM calls WHERE id = :id", {"id": call_id}))
