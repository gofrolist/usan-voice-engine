"""Phase 4c-2: the 0046 migration ships chat_analyses (columns + RLS + unique)."""

from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_chat_analyses_columns(app_session) -> None:
    rows = (
        await app_session.execute(
            text(
                "SELECT column_name, data_type, is_nullable "
                "FROM information_schema.columns WHERE table_name = 'chat_analyses'"
            )
        )
    ).all()
    cols = {r.column_name: (r.data_type, r.is_nullable) for r in rows}
    assert cols["organization_id"][0] == "uuid"
    assert cols["chat_session_id"][0] == "uuid"
    assert cols["chat_summary"] == ("text", "YES")
    assert cols["user_sentiment"] == ("text", "YES")
    assert cols["chat_successful"] == ("boolean", "YES")
    assert cols["custom_analysis_data"][0] == "jsonb"
    assert cols["model_version"][0] == "text"


@pytest.mark.asyncio
async def test_chat_analyses_rls_forced(app_session) -> None:
    row = (
        await app_session.execute(
            text(
                "SELECT relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname = 'chat_analyses'"
            )
        )
    ).one()
    assert row.relrowsecurity is True
    assert row.relforcerowsecurity is True


@pytest.mark.asyncio
async def test_chat_analyses_session_id_unique(app_session) -> None:
    cons = (
        (
            await app_session.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conrelid = 'chat_analyses'::regclass "
                    "AND contype = 'u'"
                )
            )
        )
        .scalars()
        .all()
    )
    assert any("session" in c for c in cons)
