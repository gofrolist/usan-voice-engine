"""Phase 4c-2: chat_analyses repo — upsert overwrite, batched load, cross-org RLS."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile
from usan_api.repositories import chat_analyses as repo
from usan_api.repositories import chats as chats_repo
from usan_api.tenant_context import set_tenant_context


async def _seed_session(db) -> uuid.UUID:
    profile = AgentProfile(
        name=f"Chat Agent {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "hello"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(profile)
    await db.flush()
    session = await chats_repo.add_session(
        db, agent_profile_id=profile.id, agent_version=1, dynamic_vars={}
    )
    await db.flush()
    return session.id


@pytest.mark.asyncio
async def test_upsert_inserts_then_overwrites(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid = await _seed_session(app_session)

    first = await repo.upsert(
        app_session,
        sid,
        chat_summary="first",
        user_sentiment="Neutral",
        chat_successful=False,
        custom_analysis_data=None,
        model_version="m1",
    )
    assert first.chat_summary == "first"

    second = await repo.upsert(
        app_session,
        sid,
        chat_summary="second",
        user_sentiment="Positive",
        chat_successful=True,
        custom_analysis_data=None,
        model_version="m2",
    )
    assert second.chat_summary == "second"
    assert second.user_sentiment == "Positive"
    assert second.chat_successful is True

    # Exactly one row for the session (upsert overwrote, did not duplicate).
    count = (
        await app_session.execute(
            text("SELECT count(*) FROM chat_analyses WHERE chat_session_id = :s"), {"s": sid}
        )
    ).scalar_one()
    assert count == 1
    await app_session.rollback()


@pytest.mark.asyncio
async def test_get_for_sessions_batched(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    sid1 = await _seed_session(app_session)
    sid2 = await _seed_session(app_session)
    await repo.upsert(
        app_session,
        sid1,
        chat_summary="a",
        user_sentiment=None,
        chat_successful=None,
        custom_analysis_data=None,
        model_version="m",
    )
    # sid2 has no analysis.
    got = await repo.get_for_sessions(app_session, [sid1, sid2])
    assert set(got.keys()) == {sid1}
    assert got[sid1].chat_summary == "a"
    assert await repo.get_for_sessions(app_session, []) == {}
    await app_session.rollback()


@pytest.mark.asyncio
async def test_cross_org_isolation(app_session, two_orgs) -> None:
    org_a, org_b = two_orgs
    await set_tenant_context(app_session, org_a)
    sid = await _seed_session(app_session)
    await repo.upsert(
        app_session,
        sid,
        chat_summary="secret",
        user_sentiment=None,
        chat_successful=None,
        custom_analysis_data=None,
        model_version="m",
    )
    assert await repo.get_for_session(app_session, sid) is not None

    # Switching the RLS context to org B hides org A's analysis row.
    await set_tenant_context(app_session, org_b)
    assert await repo.get_for_session(app_session, sid) is None
    await app_session.rollback()
