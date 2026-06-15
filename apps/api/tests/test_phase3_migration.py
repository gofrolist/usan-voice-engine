"""Migration 0011 creates the three Phase-3 tool tables with the right shape.

Runs against the testcontainers Postgres that conftest's `database_url` already
migrated to head. We introspect the live catalog so the test fails before 0011
exists (tables absent) and passes once it lands.
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def _columns(async_database_url: str, table: str) -> dict[str, str]:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = :t AND table_schema = 'public' "
                    "ORDER BY ordinal_position"
                ),
                {"t": table},
            )
            return {r[0]: r[1] for r in rows}
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


def test_follow_up_flags_table_shape(async_database_url):
    cols = asyncio.run(_columns(async_database_url, "follow_up_flags"))
    assert cols["id"] == "bigint"
    assert cols["call_id"] == "uuid"
    assert cols["elder_id"] == "uuid"
    assert cols["severity"] == "text"
    assert cols["category"] == "text"
    assert cols["reason"] == "text"
    assert cols["status"] == "text"
    assert cols["created_at"] == "timestamp with time zone"
    idx = asyncio.run(_indexes(async_database_url, "follow_up_flags"))
    assert "idx_followup_flags_elder" in idx
    assert "idx_followup_flags_status" in idx


def test_callback_requests_table_shape(async_database_url):
    cols = asyncio.run(_columns(async_database_url, "callback_requests"))
    assert cols["id"] == "bigint"
    assert cols["call_id"] == "uuid"
    assert cols["elder_id"] == "uuid"
    assert cols["requested_time_text"] == "text"
    assert cols["requested_at"] == "timestamp with time zone"
    assert cols["notes"] == "text"
    assert cols["status"] == "text"
    assert cols["created_at"] == "timestamp with time zone"
    idx = asyncio.run(_indexes(async_database_url, "callback_requests"))
    assert "idx_callback_requests_elder" in idx
    assert "idx_callback_requests_status" in idx


def test_sms_messages_table_shape(async_database_url):
    cols = asyncio.run(_columns(async_database_url, "sms_messages"))
    assert cols["id"] == "uuid"
    assert cols["call_id"] == "uuid"
    assert cols["elder_id"] == "uuid"
    assert cols["to_number"] == "text"
    assert cols["template_key"] == "text"
    assert cols["body"] == "text"
    assert cols["status"] == "text"
    assert cols["telnyx_message_id"] == "text"
    assert cols["error"] == "jsonb"
    assert cols["sent_at"] == "timestamp with time zone"
    assert cols["created_at"] == "timestamp with time zone"
    assert cols["updated_at"] == "timestamp with time zone"
    idx = asyncio.run(_indexes(async_database_url, "sms_messages"))
    assert "idx_sms_messages_call" in idx
    assert "idx_sms_messages_status" in idx


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


def test_follow_up_flags_cascades_call_not_elder(async_database_url):
    # CASCADE to calls(id), NO cascade to elders(id): the FK delete rules must differ.
    rules = asyncio.run(_delete_rules(async_database_url, "follow_up_flags"))
    assert rules["calls"] == "CASCADE"
    assert rules["elders"] == "NO ACTION"


async def _delete_rules_by_column(async_database_url: str, table: str) -> dict[str, str]:
    # Column-aware variant: a table may have >1 FK to the same referenced table (US8 added
    # callback_requests.dispatched_call_id -> calls alongside call_id -> calls), so key the
    # delete rule by the SOURCE column rather than the referenced table.
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(
                text(
                    "SELECT kcu.column_name AS src_col, rc.delete_rule "
                    "FROM information_schema.referential_constraints rc "
                    "JOIN information_schema.key_column_usage kcu "
                    "  ON kcu.constraint_name = rc.constraint_name "
                    "JOIN information_schema.table_constraints tc "
                    "  ON tc.constraint_name = rc.constraint_name "
                    "WHERE tc.table_name = :t AND tc.table_schema = 'public'"
                ),
                {"t": table},
            )
            return {r[0]: r[1] for r in rows}
    finally:
        await engine.dispose()


def test_callback_requests_cascades(async_database_url):
    # call_id CASCADEs to calls and elder_id is NO ACTION (mirrors follow_up_flags). The
    # US8 dispatched_call_id is a SECOND FK to calls but SET NULL — a retention purge of the
    # dialed call keeps the callback record (so the column query must be source-column-aware).
    rules = asyncio.run(_delete_rules_by_column(async_database_url, "callback_requests"))
    assert rules["call_id"] == "CASCADE"
    assert rules["elder_id"] == "NO ACTION"
    assert rules["dispatched_call_id"] == "SET NULL"


def test_sms_messages_cascades(async_database_url):
    # Same FK rules as follow_up_flags: CASCADE to calls, NO ACTION to elders.
    rules = asyncio.run(_delete_rules(async_database_url, "sms_messages"))
    assert rules["calls"] == "CASCADE"
    assert rules["elders"] == "NO ACTION"
