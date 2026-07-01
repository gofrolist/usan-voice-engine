from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime

import pytest

from usan_api.compat import ids
from usan_api.compat.errors import CompatError
from usan_api.db.models import ConversationFlowComponent
from usan_api.repositories import conversation_flow_components as repo
from usan_api.tenant_context import set_tenant_context


def test_cursor_codec_roundtrip_and_bad_input() -> None:
    cid = uuid.uuid4()
    now = datetime(2026, 6, 30, 12, 0, 0, tzinfo=UTC)
    token = ids.encode_conversation_flow_component_cursor(now, cid)
    decoded_at, decoded_id = ids.decode_conversation_flow_component_cursor(token)
    assert decoded_id == cid
    assert decoded_at == now
    with pytest.raises(CompatError):
        ids.decode_conversation_flow_component_cursor("not-valid-base64!!!")
    with pytest.raises(CompatError):
        ids.decode_conversation_flow_component_cursor(
            base64.urlsafe_b64encode(b"no-pipe-separator").decode().rstrip("=")
        )


def test_id_codec_roundtrip_and_malformed() -> None:
    cid = uuid.uuid4()
    token = ids.encode_conversation_flow_component_id(cid)
    assert token == "conversation_flow_component_" + cid.hex
    assert ids.decode_conversation_flow_component_id(token) == cid
    with pytest.raises(CompatError):
        ids.decode_conversation_flow_component_id("llm_" + cid.hex)  # wrong prefix
    with pytest.raises(CompatError):
        ids.decode_conversation_flow_component_id("conversation_flow_component_zzzz")  # bad hex


def test_id_codec_rejects_flow_prefix_collision() -> None:
    # A bare conversation_flow_ id must NOT decode as a component id (prefix is a strict superset).
    fid = uuid.uuid4()
    flow_token = ids.encode_conversation_flow_id(fid)  # "conversation_flow_<hex>"
    with pytest.raises(CompatError):
        ids.decode_conversation_flow_component_id(flow_token)


@pytest.mark.asyncio
async def test_crud_keyset_archive(two_orgs, app_session) -> None:
    org_a, _ = two_orgs
    await set_tenant_context(app_session, org_a)

    a = await repo.create(app_session, config={"name": "Collector", "nodes": []})
    b = await repo.create(app_session, config={"name": "Router", "nodes": []})
    assert isinstance(a, ConversationFlowComponent)

    got = await repo.get(app_session, a.id)
    assert got is not None
    assert got.config["name"] == "Collector"
    assert await repo.get(app_session, uuid.uuid4()) is None

    upd = await repo.update(app_session, a.id, config={"name": "Collector", "flex_mode": True})
    assert upd is not None
    assert upd.config == {"name": "Collector", "flex_mode": True}

    page = await repo.list_components(app_session, limit=10, descending=True, after=None)
    assert {c.id for c in page} >= {a.id, b.id}

    newest = page[0]
    after = await repo.list_components(
        app_session, limit=10, descending=True, after=(newest.created_at, newest.id)
    )
    assert newest.id not in {c.id for c in after}

    assert await repo.archive(app_session, a.id) is True
    assert await repo.get(app_session, a.id) is None  # archived -> excluded
    assert await repo.archive(app_session, a.id) is False  # already gone


@pytest.mark.asyncio
async def test_cross_org_isolation(two_orgs, app_session) -> None:
    org_a, org_b = two_orgs
    await set_tenant_context(app_session, org_a)
    a = await repo.create(app_session, config={"name": "A", "nodes": []})
    component_id = a.id
    await set_tenant_context(app_session, org_b)
    assert await repo.get(app_session, component_id) is None
    all_rows = await repo.list_components(app_session, limit=100, descending=True, after=None)
    assert component_id not in {c.id for c in all_rows}
