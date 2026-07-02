from __future__ import annotations

import asyncio
import time
import uuid

import jwt
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.compat import flow_runtime_voice, ids
from usan_api.db.base import CallDirection, CallStatus
from usan_api.db.models import Call, Contact, ConversationFlow
from usan_api.repositories import agent_profiles as repo
from usan_api.repositories import conversation_flows as conversation_flows_repo
from usan_api.settings import Settings, get_settings
from usan_api.tenant_context import resolve_default_org_id, set_tenant_context

_SECRET = "s" * 32


@pytest.fixture
def flow_voice_on(client):
    """Override get_settings on the routed app so flow_runtime_voice_enabled=True.

    The `client` fixture builds the routed FastAPI app lazily via `_routed_app()`
    (tests/conftest.py), which is cached at module scope and constructed the FIRST
    time any test in this worker process calls it -- `create_app()` reads
    `get_settings()` (an `lru_cache`d singleton) at that point, with the flag env var
    still unset. Because the app object (and therefore that cached Settings instance
    handed to `Depends(get_settings)`) is reused across tests, a later
    `monkeypatch.setenv("FLOW_RUNTIME_VOICE_ENABLED", "true")` in a test body never
    reaches the request handler: `get_settings()` keeps returning the stale cached
    instance until `.cache_clear()` runs, and nothing here calls that.

    This mirrors the Phase 6-runtime-chat precedent (tests/compat/conftest.py's
    `chat_analysis_on` / `gcp_project_set`): install a FastAPI `dependency_overrides`
    entry on the actual app object the `client` TestClient wraps, so
    `Depends(get_settings)` observes the flag directly, independent of the
    `lru_cache` state.
    """
    base = get_settings()

    def _override() -> Settings:
        return base.model_copy(update={"flow_runtime_voice_enabled": True})

    client.app.dependency_overrides[get_settings] = _override
    yield
    client.app.dependency_overrides.pop(get_settings, None)


def _worker_token() -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "iat": now, "exp": now + 300}, _SECRET, algorithm="HS256"
    )


def _wauth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_worker_token()}"}


def _two_node_flow() -> dict:
    return {
        "start_node_id": "n1",
        "global_prompt": "You are Ann's assistant.",
        "nodes": [
            {
                "id": "n1",
                "type": "conversation",
                "instruction": {"type": "prompt", "text": "Greet the caller."},
                "edges": [
                    {
                        "id": "e1",
                        "transition_condition": {"type": "prompt", "prompt": "Always"},
                        "destination_node_id": "n2",
                    }
                ],
            },
            {"id": "n2", "type": "end", "instruction": {"type": "prompt", "text": "Say goodbye."}},
        ],
    }


async def _seed(async_url: str, *, flow_config: dict, bind: bool, same_org: bool = True) -> dict:
    """Seed org+profile(+binding)+flow+call. Returns ids as strings.

    Mirrors tests/test_runtime.py's _publish_profile/_seed_call_with_contact idiom
    (create_async_engine + async_sessionmaker driven by asyncio.run, raw model
    construction for Contact/Call). bind=True writes compat_response_engine into the
    published version's config (agent_profiles_repo.publish() snapshots
    profile.draft_config, so the binding must land in the draft BEFORE publish() —
    same sequencing test_runtime.py's _publish_profile uses for the voice-id field).

    same_org=False creates the conversation_flow under a brand-new organization (raw
    INSERT INTO organizations, mirroring tests/test_rls_isolation.py's _seed_two_orgs)
    and sets ConversationFlow.organization_id explicitly to it — bypassing
    conversation_flows_repo.create's server_default (which would otherwise resolve to
    the default org, same as the call). The seeding engine connects as the Postgres
    superuser (async_database_url), so RLS never blocks this seed regardless of which
    org is targeted; only the app-role request path (the `client` fixture) is
    RLS-subject, which is what makes the cross-org test meaningful.
    """
    engine = create_async_engine(async_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    flow_id: str | None = None
    try:
        async with factory() as db:
            profile = await repo.create_profile(
                db, name=f"p-{uuid.uuid4().hex}", description=None, actor_email="op"
            )
            cfg = dict(profile.draft_config)
            if bind:
                if same_org:
                    flow = await conversation_flows_repo.create(db, config=flow_config)
                else:
                    other_org_id = (
                        await db.execute(
                            text(
                                "INSERT INTO organizations (name, slug) "
                                "VALUES ('other', :s) RETURNING id"
                            ),
                            {"s": f"o-{uuid.uuid4().hex[:8]}"},
                        )
                    ).scalar_one()
                    flow = ConversationFlow(config=flow_config, organization_id=other_org_id)
                    db.add(flow)
                    await db.flush()
                    await db.refresh(flow)
                flow_id = str(flow.id)
                cfg["compat_response_engine"] = {
                    "type": "conversation-flow",
                    "conversation_flow_id": ids.encode_conversation_flow_id(flow.id),
                }
            await repo.update_draft(db, profile.id, config=cfg, description=None, actor_email="op")
            await repo.publish(db, profile.id, note="v1", actor_email="op")
            contact = Contact(
                name="Ann",
                phone_e164=f"+1555{uuid.uuid4().int % 10**7:07d}",
                timezone="America/New_York",
                agent_profile_id=profile.id,
            )
            db.add(contact)
            await db.flush()
            call = Call(
                contact_id=contact.id,
                direction=CallDirection.OUTBOUND,
                status=CallStatus.IN_PROGRESS,
            )
            db.add(call)
            await db.flush()
            call_id = str(call.id)
            await db.commit()
            return {"call_id": call_id, "flow_id": flow_id}
    finally:
        await engine.dispose()


def _run(coro):
    return asyncio.run(coro)


def test_flag_off_returns_unbound(client, async_database_url, monkeypatch) -> None:
    monkeypatch.setenv("FLOW_RUNTIME_VOICE_ENABLED", "false")
    seeded = _run(_seed(async_database_url, flow_config=_two_node_flow(), bind=True))
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": seeded["call_id"], "cursor": None, "turns": []},
        headers=_wauth(),
    )
    assert resp.status_code == 200
    assert resp.json()["bound"] is False


def test_bound_enters_start_node(client, async_database_url, flow_voice_on) -> None:
    seeded = _run(_seed(async_database_url, flow_config=_two_node_flow(), bind=True))
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": seeded["call_id"], "cursor": None, "turns": []},
        headers=_wauth(),
    )
    body = resp.json()
    assert body["bound"] is True
    assert body["node_id"] == "n1"
    # cursor is the opaque, flow-qualified token the agent must round-trip next turn.
    assert body["cursor"] == f"{seeded['flow_id']}:0:n1"
    assert "Greet the caller." in body["instruction"]
    assert "You are Ann's assistant." in body["instruction"]
    assert body["is_end"] is False


def test_always_edge_advances_to_end(client, async_database_url, flow_voice_on) -> None:
    seeded = _run(_seed(async_database_url, flow_config=_two_node_flow(), bind=True))
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={
            "call_id": seeded["call_id"],
            "cursor": f"{seeded['flow_id']}:0:n1",
            "turns": [{"role": "user", "content": "ok bye"}],
        },
        headers=_wauth(),
    )
    body = resp.json()
    assert body["bound"] is True
    assert body["node_id"] == "n2"
    assert body["cursor"] == f"{seeded['flow_id']}:0:n2"
    assert body["is_end"] is True


def test_stale_cursor_reenters_start(client, async_database_url, flow_voice_on) -> None:
    seeded = _run(_seed(async_database_url, flow_config=_two_node_flow(), bind=True))
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": seeded["call_id"], "cursor": f"{seeded['flow_id']}:0:ghost", "turns": []},
        headers=_wauth(),
    )
    body = resp.json()
    assert body["node_id"] == "n1"
    assert body["cursor"] == f"{seeded['flow_id']}:0:n1"


def test_repoint_to_different_flow_reenters_start_not_colliding_node(
    client, async_database_url, flow_voice_on
) -> None:
    # A cursor carrying a DIFFERENT flow uuid than the currently-bound flow, but a node id that
    # also exists in the current flow, must NOT resolve against the colliding node — it must
    # re-enter start_node_id. This guards against a repoint (Phase 6c update-agent) leaving a
    # stale same-named-node cursor from the old flow.
    seeded = _run(_seed(async_database_url, flow_config=_two_node_flow(), bind=True))
    other_flow_uuid = uuid.uuid4()
    stale_cursor = f"{other_flow_uuid}:0:n2"  # "n2" collides with a real node in the bound flow
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": seeded["call_id"], "cursor": stale_cursor, "turns": []},
        headers=_wauth(),
    )
    body = resp.json()
    assert body["bound"] is True
    assert body["node_id"] == "n1"
    assert body["cursor"] == f"{seeded['flow_id']}:0:n1"
    assert body["is_end"] is False


def test_unbound_call_returns_unbound(client, async_database_url, flow_voice_on) -> None:
    seeded = _run(_seed(async_database_url, flow_config=_two_node_flow(), bind=False))
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": seeded["call_id"], "cursor": None, "turns": []},
        headers=_wauth(),
    )
    assert resp.json()["bound"] is False


def test_unrunnable_flow_returns_unbound(client, async_database_url, flow_voice_on) -> None:
    flow = _two_node_flow()
    flow["nodes"][0]["type"] = "function"  # unsupported node type -> not runnable
    seeded = _run(_seed(async_database_url, flow_config=flow, bind=True))
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": seeded["call_id"], "cursor": None, "turns": []},
        headers=_wauth(),
    )
    assert resp.json()["bound"] is False


def test_cross_org_flow_binding_is_unbound(
    client, async_database_url, app_async_database_url, app_role_password, monkeypatch
) -> None:
    # The flow lives under a DIFFERENT org than the call: RLS makes it indistinguishable
    # from absent -> bound=False (never leaks, never errors).
    #
    # This deliberately bypasses the `client`/HTTP path: the `client` fixture's Depends(get_db)
    # override runs on the superuser test engine (see tests/conftest.py's `client` fixture and
    # the `_install_compat_db_override` docstring — "Cross-org RLS isolation is exercised
    # separately... a real usan_app session"), and Postgres superusers always bypass row
    # security regardless of policy, so a request through `client` cannot exercise RLS here.
    # Instead this calls flow_runtime_voice.advance directly against a real usan_app
    # (non-superuser, RLS-subject) session scoped to the call's own org — the same resolver
    # code the endpoint calls, under genuine row-level security. Mirrors the pattern in
    # tests/test_compat_rls_isolation.py.
    monkeypatch.setenv("FLOW_RUNTIME_VOICE_ENABLED", "true")
    seeded = _run(
        _seed(async_database_url, flow_config=_two_node_flow(), bind=True, same_org=False)
    )
    settings = get_settings()

    async def _check() -> flow_runtime_voice.FlowAdvanceResponse:
        engine = create_async_engine(app_async_database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                org_id = await resolve_default_org_id(db)
                await set_tenant_context(db, org_id)
                return await flow_runtime_voice.advance(
                    db, settings, uuid.UUID(seeded["call_id"]), None, []
                )
        finally:
            await engine.dispose()

    result = _run(_check())
    assert result.bound is False


def test_missing_call_returns_unbound(client, async_database_url, monkeypatch) -> None:
    monkeypatch.setenv("FLOW_RUNTIME_VOICE_ENABLED", "true")
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": str(uuid.uuid4()), "cursor": None, "turns": []},
        headers=_wauth(),
    )
    assert resp.json()["bound"] is False


def test_requires_worker_token(client, async_database_url) -> None:
    resp = client.post(
        "/v1/runtime/flow-advance",
        json={"call_id": str(uuid.uuid4()), "cursor": None, "turns": []},
    )
    assert resp.status_code == 401
