"""Unit test for the shared KB text-source helper (Task 1)."""

import pytest

from usan_api.compat.kb_sources import TextSource, add_text_sources
from usan_api.repositories import knowledge_bases as repo
from usan_api.tenant_context import set_tenant_context


@pytest.mark.asyncio
async def test_add_text_sources_persists_and_marks_in_progress(two_orgs, app_session):
    org_a, _org_b = two_orgs
    await set_tenant_context(app_session, org_a)
    kb = await repo.create_kb(
        app_session, name="KB", max_chunk_size=2000, min_chunk_size=400, enable_auto_refresh=False
    )
    # Simulate ingestion having completed so we can prove the helper resets it.
    await repo.set_status(app_session, kb.id, "complete")

    created = await add_text_sources(
        app_session, kb.id, [TextSource(title="Doc A", text="hello world")]
    )

    sources = await repo.get_sources(app_session, kb.id)
    assert len(sources) == 1
    assert created == [sources[0].id]  # helper returns the created source ids (for auditing)
    assert sources[0].title == "Doc A"
    assert sources[0].content == "hello world"
    assert sources[0].content_url  # non-empty internal reference set by the helper
    refreshed = await repo.get_kb(app_session, kb.id)
    assert refreshed is not None
    assert refreshed.status == "in_progress"  # reset for re-ingestion


@pytest.mark.asyncio
async def test_add_text_sources_empty_is_noop(two_orgs, app_session):
    org_a, _org_b = two_orgs
    await set_tenant_context(app_session, org_a)
    kb = await repo.create_kb(
        app_session, name="KB2", max_chunk_size=2000, min_chunk_size=400, enable_auto_refresh=False
    )
    await repo.set_status(app_session, kb.id, "complete")

    assert await add_text_sources(app_session, kb.id, []) == []

    assert not await repo.get_sources(app_session, kb.id)
    refreshed = await repo.get_kb(app_session, kb.id)
    assert refreshed is not None
    assert refreshed.status == "complete"  # untouched when nothing added
