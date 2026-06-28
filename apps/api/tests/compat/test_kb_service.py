import uuid

import pytest

from usan_api.compat import ids, kb_service
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.knowledge_bases import KbTextInput, ParsedKbAddSources, ParsedKbCreate
from usan_api.repositories import knowledge_bases as repo
from usan_api.tenant_context import resolve_default_org_id, set_tenant_context


async def _ctx(app_session):
    org = await resolve_default_org_id(app_session)
    await set_tenant_context(app_session, org)


@pytest.mark.asyncio
async def test_create_persists_sources_in_progress(app_session) -> None:
    await _ctx(app_session)
    parsed = ParsedKbCreate(name="kb", texts=[KbTextInput(title="t", text="body")])
    kb = await kb_service.create_kb(app_session, parsed)
    await _ctx(app_session)
    assert kb.status == "in_progress"
    srcs = await repo.get_sources(app_session, kb.id)
    assert len(srcs) == 1
    assert srcs[0].content_url.startswith("https://knowledge-base.internal/source/source_")


@pytest.mark.asyncio
async def test_create_rejects_files_and_bad_chunks(app_session) -> None:
    await _ctx(app_session)
    with pytest.raises(CompatError) as e1:
        await kb_service.create_kb(app_session, ParsedKbCreate(name="kb", has_files=True))
    assert e1.value.status_code == 422
    with pytest.raises(CompatError):
        await kb_service.create_kb(
            app_session, ParsedKbCreate(name="kb", min_chunk_size=2000, max_chunk_size=2000)
        )
    with pytest.raises(CompatError):
        await kb_service.create_kb(app_session, ParsedKbCreate(name="x" * 40))


@pytest.mark.asyncio
async def test_add_sources_resets_in_progress(app_session) -> None:
    await _ctx(app_session)
    kb = await kb_service.create_kb(app_session, ParsedKbCreate(name="kb"))
    await repo.set_status(app_session, kb.id, "complete")
    await app_session.commit()
    await _ctx(app_session)
    kb2 = await kb_service.add_sources(
        app_session,
        ids.encode_kb_id(kb.id),
        ParsedKbAddSources(texts=[KbTextInput(title="t", text="more")]),
    )
    assert kb2.status == "in_progress"


@pytest.mark.asyncio
async def test_delete_kb_404(app_session) -> None:
    await _ctx(app_session)
    with pytest.raises(CompatError) as e:
        await kb_service.delete_kb(app_session, ids.encode_kb_id(uuid.uuid4()))
    assert e.value.status_code == 404
