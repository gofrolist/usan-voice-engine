import pytest

from usan_api import settings as settings_mod
from usan_api.compat import kb_ingestion
from usan_api.repositories import knowledge_bases as repo
from usan_api.tenant_context import resolve_default_org_id, set_tenant_context


def _on():
    return settings_mod.get_settings().model_copy(
        update={"kb_embedding_enabled": True, "gcp_project": "p"}
    )


async def _seed(app_session, *, content="hello world body"):
    org = await resolve_default_org_id(app_session)
    await set_tenant_context(app_session, org)
    kb = await repo.create_kb(
        app_session, name="k", max_chunk_size=2000, min_chunk_size=2, enable_auto_refresh=False
    )
    await repo.add_source(
        app_session, kb.id, source_type="text", title="t", content=content, content_url="u"
    )
    return kb


@pytest.mark.asyncio
async def test_ingest_completes_and_chunks(app_session, mock_embed) -> None:
    kb = await _seed(app_session)
    await kb_ingestion.ingest_one_kb(app_session, kb.id, _on())
    await app_session.commit()
    org = await resolve_default_org_id(app_session)
    await set_tenant_context(app_session, org)
    assert (await repo.get_kb(app_session, kb.id)).status == "complete"
    assert await repo.get_unchunked_sources(app_session, kb.id) == []


@pytest.mark.asyncio
async def test_ingest_embed_failure_sets_error(app_session, monkeypatch) -> None:
    kb = await _seed(app_session)

    async def _boom(texts, settings):
        raise RuntimeError("vertex down")

    monkeypatch.setattr("usan_api.compat.kb_ingestion.embed_texts", _boom)
    await kb_ingestion.ingest_one_kb(app_session, kb.id, _on())
    await app_session.commit()
    org = await resolve_default_org_id(app_session)
    await set_tenant_context(app_session, org)
    kb2 = await repo.get_kb(app_session, kb.id)
    assert kb2.status == "error"
    assert kb2.error_detail == "RuntimeError"


@pytest.mark.asyncio
async def test_ingest_disabled_is_noop(app_session, mock_embed) -> None:
    kb = await _seed(app_session)
    await kb_ingestion.ingest_one_kb(app_session, kb.id, settings_mod.get_settings())  # flag off
    await app_session.commit()
    org = await resolve_default_org_id(app_session)
    await set_tenant_context(app_session, org)
    assert (await repo.get_kb(app_session, kb.id)).status == "in_progress"
