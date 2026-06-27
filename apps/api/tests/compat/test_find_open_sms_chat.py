"""find_open_sms_chat + add_message(provider_message_id) (Phase 4b-2)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from usan_api.db.base import ChatStatus, ProfileStatus
from usan_api.db.models import AgentProfile, ChatSession
from usan_api.repositories import chats as chats_repo
from usan_api.tenant_context import set_tenant_context

_OUR = "+15550000000"
_RECIP = "+15551234567"


async def _profile(db) -> AgentProfile:
    p = AgentProfile(
        name=f"A {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "x"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(p)
    await db.flush()
    return p


async def _sms_session(db, profile, *, frm, to, status=ChatStatus.ONGOING, chat_type="sms_chat"):
    s = ChatSession(
        agent_profile_id=profile.id,
        agent_version=1,
        chat_type=chat_type,
        dynamic_vars={},
        from_number=frm,
        to_number=to,
        status=status,
    )
    db.add(s)
    await db.flush()
    return s


@pytest.mark.asyncio
async def test_matches_open_sms_chat(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    p = await _profile(app_session)
    want = await _sms_session(app_session, p, frm=_OUR, to=_RECIP)
    # decoys: ended, archived, wrong number, api_chat
    await _sms_session(app_session, p, frm=_OUR, to=_RECIP, status=ChatStatus.ENDED)
    await _sms_session(app_session, p, frm=_OUR, to="+19998887777")
    await _sms_session(app_session, p, frm=_OUR, to=_RECIP, chat_type="api_chat")

    got = await chats_repo.find_open_sms_chat(app_session, our_number=_OUR, recipient=_RECIP)
    assert got is not None
    assert got.id == want.id
    await app_session.rollback()


@pytest.mark.asyncio
async def test_no_match_returns_none(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    got = await chats_repo.find_open_sms_chat(app_session, our_number=_OUR, recipient=_RECIP)
    assert got is None
    await app_session.rollback()


@pytest.mark.asyncio
async def test_multiple_open_picks_newest(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    p = await _profile(app_session)
    old = await _sms_session(app_session, p, frm=_OUR, to=_RECIP)
    # force a later started_at on the second row
    new = await _sms_session(app_session, p, frm=_OUR, to=_RECIP)
    await app_session.execute(
        text("UPDATE chat_sessions SET started_at = now() + interval '1 hour' WHERE id = :i"),
        {"i": new.id},
    )
    got = await chats_repo.find_open_sms_chat(app_session, our_number=_OUR, recipient=_RECIP)
    assert got is not None
    assert got.id == new.id
    assert got.id != old.id
    await app_session.rollback()


@pytest.mark.asyncio
async def test_add_message_persists_provider_id(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    p = await _profile(app_session)
    s = await _sms_session(app_session, p, frm=_OUR, to=_RECIP)
    m = await chats_repo.add_message(
        app_session, session_id=s.id, seq=1, role="sms", content="hi", provider_message_id="tx-9"
    )
    await app_session.flush()
    assert m.provider_message_id == "tx-9"
    await app_session.rollback()
