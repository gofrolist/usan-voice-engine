from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from usan_api.compat import ids
from usan_api.db.models import ConversationFlow
from usan_api.repositories import conversation_flows as repo
from usan_api.tenant_context import set_tenant_context


def test_cursor_codec_roundtrip() -> None:
    fid = uuid.uuid4()
    now = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)
    token = ids.encode_conversation_flow_cursor(now, fid)
    decoded_at, decoded_id = ids.decode_conversation_flow_cursor(token)
    assert decoded_id == fid
    assert decoded_at == now


def test_id_codec_roundtrip() -> None:
    fid = uuid.uuid4()
    token = ids.encode_conversation_flow_id(fid)
    assert token == "conversation_flow_" + fid.hex
    assert ids.decode_conversation_flow_id(token) == fid


@pytest.mark.asyncio
async def test_crud_keyset_archive(two_orgs, app_session) -> None:
    org_a, _ = two_orgs
    await set_tenant_context(app_session, org_a)

    a = await repo.create(app_session, config={"start_speaker": "agent", "nodes": []})
    b = await repo.create(app_session, config={"start_speaker": "user", "nodes": []})
    assert isinstance(a, ConversationFlow)
    assert a.version == 0

    got = await repo.get(app_session, a.id)
    assert got is not None
    assert got.config["start_speaker"] == "agent"
    assert await repo.get(app_session, uuid.uuid4()) is None

    upd = await repo.update(
        app_session, a.id, config={"start_speaker": "agent", "global_prompt": "hi"}, version=1
    )
    assert upd is not None
    assert upd.version == 1
    assert upd.config == {"start_speaker": "agent", "global_prompt": "hi"}

    page = await repo.list_flows(app_session, limit=10, descending=True, after=None)
    assert {f.id for f in page} >= {a.id, b.id}

    newest = page[0]
    after = await repo.list_flows(
        app_session, limit=10, descending=True, after=(newest.created_at, newest.id)
    )
    assert newest.id not in {f.id for f in after}

    assert await repo.archive(app_session, a.id) is True
    assert await repo.get(app_session, a.id) is None  # archived -> excluded
    assert await repo.archive(app_session, a.id) is False  # already gone


@pytest.mark.asyncio
async def test_cross_org_isolation(two_orgs, app_session) -> None:
    org_a, org_b = two_orgs
    await set_tenant_context(app_session, org_a)
    a = await repo.create(app_session, config={"nodes": []})
    flow_id = a.id
    # Switch to org B (non-superuser app_session is RLS-bound) -> A's flow is invisible.
    await set_tenant_context(app_session, org_b)
    assert await repo.get(app_session, flow_id) is None
    all_flows = await repo.list_flows(app_session, limit=100, descending=True, after=None)
    assert flow_id not in {f.id for f in all_flows}
