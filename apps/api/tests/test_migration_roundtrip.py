"""One shared downgrade → seed → upgrade round-trip for migrations 0012–0015.

Consolidates the four per-module round-trips that used to live in
test_batch_migration (→0011), test_ops_queue_migration (→0012),
test_webhook_migration (→0013) and test_custom_variables_migration (→0014).
Each walked its own full head→rev→head chain: 324 migration steps and 8 alembic
subprocesses in total, making them the four slowest tests in CI. Their
downgrade targets are consecutive and every pre-state asserted below already
holds at 0011, so ONE head→0011→head cycle (2 subprocesses, ~84 steps)
exercises the union — including the spec §10.1 property that upgrading over a
*populated* pre-upgrade database works, now with all four suites' seed data
present in the same upgrade.

The per-migration head-state shape tests (columns/indexes/checks/FK rules)
still live in their original modules.

All introspection/seed/cleanup statements run batched on one engine per phase —
the old modules created a throwaway engine per statement (~25 per test).
"""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import _set_app_role_password

API_DIR = Path(__file__).resolve().parents[1]
TEST_SECRET = "a" * 32


async def _columns(conn: AsyncConnection, table: str) -> dict[str, Any]:
    rows = await conn.execute(
        text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = :t AND table_schema = 'public'"
        ),
        {"t": table},
    )
    return {r[0]: r[1] for r in rows}


async def _indexes(conn: AsyncConnection, table: str) -> set[str]:
    rows = await conn.execute(
        text("SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": table}
    )
    return {r[0] for r in rows}


async def _check_constraints(conn: AsyncConnection, table: str) -> set[str]:
    rows = await conn.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = CAST(:t AS regclass) AND contype = 'c'"
        ),
        {"t": table},
    )
    return {r[0] for r in rows}


_ELDER_ID = uuid.uuid4()
_OPS_CALL_ID = uuid.uuid4()  # carries the stray 'weird' ops statuses
_WH_CALL_ID = uuid.uuid4()  # the plain populated-DB row (webhook suite)


async def _assert_pre_state_and_seed(async_database_url: str) -> None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            # 0012 (batch/scheduled-calling) not applied yet.
            assert await _columns(conn, "call_schedules") == {}
            assert await _columns(conn, "call_batches") == {}
            assert await _columns(conn, "call_batch_targets") == {}
            calls_idx = await _indexes(conn, "calls")
            assert "idx_calls_in_flight" not in calls_idx

            # 0013 (ops-queue status workflow) not applied yet.
            for table in ("follow_up_flags", "callback_requests"):
                cols = await _columns(conn, table)
                assert "status_updated_at" not in cols
                assert "status_updated_by" not in cols
            assert "ck_follow_up_flags_status" not in await _check_constraints(
                conn, "follow_up_flags"
            )
            assert "ck_callback_requests_status" not in await _check_constraints(
                conn, "callback_requests"
            )
            assert "idx_calls_created" not in calls_idx

            # 0014 (outbound webhooks) and 0015 (custom variables) not applied yet.
            assert await _columns(conn, "webhook_endpoints") == {}
            assert await _columns(conn, "webhook_deliveries") == {}
            assert await _columns(conn, "custom_variables") == {}

            # Seed while downgraded, with the era's names (elders — renamed to
            # contacts by 0027) and a stray non-enum ops status — legal pre-CHECK.
            await conn.execute(
                text(
                    "INSERT INTO elders (id, name, phone_e164, timezone) "
                    "VALUES (:id, 'Mig Roundtrip', '+19998880014', 'America/New_York')"
                ),
                {"id": _ELDER_ID},
            )
            await conn.execute(
                text(
                    "INSERT INTO calls (id, elder_id, direction, status) "
                    "VALUES (:id, :elder_id, CAST('outbound' AS call_direction), "
                    "CAST('queued' AS call_status))"
                ),
                {"id": _OPS_CALL_ID, "elder_id": _ELDER_ID},
            )
            await conn.execute(
                text(
                    "INSERT INTO follow_up_flags (call_id, elder_id, severity, category, status) "
                    "VALUES (:call_id, :elder_id, 'routine', 'medical', 'weird')"
                ),
                {"call_id": _OPS_CALL_ID, "elder_id": _ELDER_ID},
            )
            await conn.execute(
                text(
                    "INSERT INTO callback_requests "
                    "(call_id, elder_id, requested_time_text, status) "
                    "VALUES (:call_id, :elder_id, 'x', 'weird')"
                ),
                {"call_id": _OPS_CALL_ID, "elder_id": _ELDER_ID},
            )
            await conn.execute(
                text(
                    "INSERT INTO calls (id, direction, status) "
                    "VALUES (:id, CAST('outbound' AS call_direction), "
                    "CAST('queued' AS call_status))"
                ),
                {"id": _WH_CALL_ID},
            )
    finally:
        await engine.dispose()


async def _assert_post_state_and_cleanup(async_database_url: str) -> None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            # 0012: batch tables and the in-flight gate index are back.
            for table in ("call_schedules", "call_batches", "call_batch_targets"):
                assert "id" in await _columns(conn, table)
            calls_idx = await _indexes(conn, "calls")
            assert "idx_calls_in_flight" in calls_idx

            # 0013: the defensive normalize rewrote the stray statuses before the
            # CHECK landed, and the workflow columns/constraints exist.
            for table in ("follow_up_flags", "callback_requests"):
                cols = await _columns(conn, table)
                assert "status_updated_at" in cols
                assert "status_updated_by" in cols
                status = (
                    await conn.execute(
                        text(f"SELECT status FROM {table} WHERE call_id = :call_id"),  # noqa: S608
                        {"call_id": _OPS_CALL_ID},
                    )
                ).scalar_one()
                assert status == "open"
            assert "ck_follow_up_flags_status" in await _check_constraints(conn, "follow_up_flags")
            assert "ck_callback_requests_status" in await _check_constraints(
                conn, "callback_requests"
            )
            assert "idx_calls_created" in calls_idx

            # 0014: webhook tables re-created; the seeded business row survived.
            assert "id" in await _columns(conn, "webhook_endpoints")
            assert "id" in await _columns(conn, "webhook_deliveries")
            assert "idx_webhook_deliveries_due" in await _indexes(conn, "webhook_deliveries")
            survived = (
                await conn.execute(
                    text("SELECT count(*) FROM calls WHERE id = :id"), {"id": _WH_CALL_ID}
                )
            ).scalar_one()
            assert survived == 1

            # 0015: custom_variables re-created with its slug CHECK.
            assert "id" in await _columns(conn, "custom_variables")
            assert "ck_custom_variables_name_slug" in await _check_constraints(
                conn, "custom_variables"
            )

            # Cleanup: the seeded `elders` row is now a `contacts` row (0027 rename).
            await conn.execute(
                text("DELETE FROM follow_up_flags WHERE call_id = :id"), {"id": _OPS_CALL_ID}
            )
            await conn.execute(
                text("DELETE FROM callback_requests WHERE call_id = :id"), {"id": _OPS_CALL_ID}
            )
            await conn.execute(
                text("DELETE FROM calls WHERE id IN (:a, :b)"),
                {"a": _OPS_CALL_ID, "b": _WH_CALL_ID},
            )
            await conn.execute(text("DELETE FROM contacts WHERE id = :id"), {"id": _ELDER_ID})
    finally:
        await engine.dispose()


def test_downgrade_seed_upgrade_roundtrip(database_url: str, async_database_url: str) -> None:
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
        [sys.executable, "-m", "alembic", "downgrade", "0011"],
        cwd=API_DIR,
        env=env,
        check=True,
    )
    try:
        asyncio.run(_assert_pre_state_and_seed(async_database_url))
    finally:
        # A mid-test failure must not strand the shared session DB below head —
        # every other test on this worker assumes head.
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=API_DIR,
            env=env,
            check=True,
        )
        # The downgrade dropped and the re-upgrade recreated the usan_app role
        # PASSWORDLESS (0029 carries no secrets). Heal it here — this test is the
        # only one in the suite that downgrades below 0029, so healing in ITS
        # finally keeps every later usan_app connection on this worker working
        # without a per-test ALTER ROLE elsewhere.
        asyncio.run(_set_app_role_password(database_url))

    asyncio.run(_assert_post_state_and_cleanup(async_database_url))
