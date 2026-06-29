"""Phase 5b: bind + validate knowledge_base_ids on create/update-retell-llm."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.compat import agent_bridge
from usan_api.compat.errors import CompatError
from usan_api.compat.ids import encode_kb_id
from usan_api.settings import Settings
from usan_api.tenant_context import set_tenant_context


def _settings() -> Settings:
    return Settings(
        DATABASE_URL="postgresql://u:p@host/db",
        LIVEKIT_API_KEY="key",
        LIVEKIT_API_SECRET="a" * 32,
        LIVEKIT_URL="ws://livekit:7880",
        JWT_SIGNING_KEY="s" * 32,
        OPERATOR_API_KEY="o" * 32,
        GCP_PROJECT="test-project",
    )


async def _org(app_session):
    """Resolve an org id and install an after_begin listener that re-applies the RLS
    context after each COMMIT (set_config is_local=true is transaction-scoped; functions
    like create_response_engine commit internally then call db.refresh — without the
    re-apply the post-commit SELECT runs context-free and RLS hides the row)."""
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    org_str = str(org_id)

    def _reapply(_session, _transaction, connection) -> None:
        connection.execute(
            text("SELECT set_config('app.current_org', :org, true)"), {"org": org_str}
        )

    event.listen(app_session.sync_session, "after_begin", _reapply)
    await set_tenant_context(app_session, org_id)
    return org_id


async def _seed_kb_for_org(super_url: str, org_id: uuid.UUID, name: str) -> uuid.UUID:
    """Insert a KB directly (superuser bypasses RLS) for an arbitrary org; returns its id."""
    engine = create_async_engine(super_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        "INSERT INTO knowledge_bases "
                        "(organization_id, name, status, max_chunk_size, min_chunk_size) "
                        "VALUES (:org, :name, 'in_progress', 2000, 400) RETURNING id"
                    ),
                    {"org": str(org_id), "name": name},
                )
            ).one()
            return row[0]
    finally:
        await engine.dispose()


async def _delete_kbs_for_org(super_url: str, org_id: uuid.UUID) -> None:
    """Delete all KB rows for an org (superuser) so org teardown FK constraint is satisfied."""
    engine = create_async_engine(super_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM knowledge_bases WHERE organization_id = :org"),
                {"org": str(org_id)},
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_retell_llm_rejects_unknown_kb_id(app_session) -> None:
    from usan_api.compat.schemas.retell_llm import CreateRetellLlmRequest

    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    body = CreateRetellLlmRequest(
        general_prompt="hi",
        knowledge_base_ids=[encode_kb_id(uuid.uuid4())],  # well-formed, absent
    )
    with pytest.raises(CompatError) as ei:
        await agent_bridge.create_response_engine(app_session, _settings(), body)
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_create_retell_llm_rejects_malformed_kb_id(app_session) -> None:
    from usan_api.compat.schemas.retell_llm import CreateRetellLlmRequest

    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    body = CreateRetellLlmRequest(general_prompt="hi", knowledge_base_ids=["not-a-kb-id"])
    with pytest.raises(CompatError) as ei:
        await agent_bridge.create_response_engine(app_session, _settings(), body)
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_create_retell_llm_rejects_cross_org_kb_id(
    two_orgs, app_session, async_database_url
) -> None:
    from usan_api.compat.schemas.retell_llm import CreateRetellLlmRequest

    org_a, org_b = two_orgs
    kb_b = await _seed_kb_for_org(async_database_url, org_b, "kb-b")
    try:
        await set_tenant_context(app_session, org_a)
        body = CreateRetellLlmRequest(general_prompt="hi", knowledge_base_ids=[encode_kb_id(kb_b)])
        with pytest.raises(CompatError) as ei:
            await agent_bridge.create_response_engine(app_session, _settings(), body)
        assert ei.value.status_code == 422
    finally:
        await _delete_kbs_for_org(async_database_url, org_b)


@pytest.mark.asyncio
async def test_create_retell_llm_persists_and_echoes_valid_kb_id(app_session) -> None:
    from usan_api.compat.schemas.retell_llm import CreateRetellLlmRequest
    from usan_api.repositories import knowledge_bases as kb_repo

    await _org(app_session)
    kb = await kb_repo.create_kb(
        app_session, name="kb", max_chunk_size=2000, min_chunk_size=400, enable_auto_refresh=False
    )
    await app_session.commit()
    # commit cleared is_local context; _org re-listener handles re-apply via after_begin
    kb_token = encode_kb_id(kb.id)
    profile = await agent_bridge.create_response_engine(
        app_session,
        _settings(),
        CreateRetellLlmRequest(general_prompt="hi", knowledge_base_ids=[kb_token]),
    )
    # native config feeds generation
    assert profile.draft_config["llm"]["knowledge_base_ids"] == [kb_token]
    # echo path unchanged
    resp = agent_bridge.serialize_llm(profile)
    assert resp.model_dump().get("knowledge_base_ids") == [kb_token]


@pytest.mark.asyncio
async def test_update_retell_llm_rejects_unknown_kb_id(app_session) -> None:
    """update_response_engine (PATCH) with an absent but well-formed kb id -> 422."""
    from usan_api.compat.ids import encode_llm_id
    from usan_api.compat.schemas.retell_llm import CreateRetellLlmRequest, UpdateRetellLlmRequest

    await _org(app_session)
    profile = await agent_bridge.create_response_engine(
        app_session,
        _settings(),
        CreateRetellLlmRequest(general_prompt="hello"),
    )
    llm_token = encode_llm_id(profile.id)

    with pytest.raises(CompatError) as ei:
        await agent_bridge.update_response_engine(
            app_session,
            _settings(),
            llm_token,
            UpdateRetellLlmRequest(knowledge_base_ids=[encode_kb_id(uuid.uuid4())]),
        )
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_update_retell_llm_preserves_kb_binding_on_noop(app_session) -> None:
    """PATCH with knowledge_base_ids=None (omitted) must leave a prior binding intact."""
    from usan_api.compat.ids import encode_llm_id
    from usan_api.compat.schemas.retell_llm import CreateRetellLlmRequest, UpdateRetellLlmRequest
    from usan_api.repositories import knowledge_bases as kb_repo

    await _org(app_session)
    kb = await kb_repo.create_kb(
        app_session,
        name="kb-noop",
        max_chunk_size=2000,
        min_chunk_size=400,
        enable_auto_refresh=False,
    )
    await app_session.commit()
    # after_begin listener re-applies RLS context after commit
    kb_token = encode_kb_id(kb.id)

    # Create the LLM bound to the KB
    profile = await agent_bridge.create_response_engine(
        app_session,
        _settings(),
        CreateRetellLlmRequest(general_prompt="hi", knowledge_base_ids=[kb_token]),
    )
    llm_token = encode_llm_id(profile.id)

    # PATCH with an unrelated field change — knowledge_base_ids is absent (None)
    updated = await agent_bridge.update_response_engine(
        app_session,
        _settings(),
        llm_token,
        UpdateRetellLlmRequest(general_prompt="updated prompt"),
    )
    # The prior KB binding must survive the no-op patch
    assert updated.draft_config["llm"]["knowledge_base_ids"] == [kb_token]
