"""T031 — contract + integration tests for the RetellAI-compatible agent &
response-engine (Retell-LLM) endpoints (feature 003, US3).

Driven over the real HTTP path against the mounted compat sub-app (the
``compat_client`` + ``compat_headers`` fixtures, mirroring ``test_compat_calls.py``):
``create-retell-llm`` -> ``create-agent`` (which binds the agent half to the SAME
AgentProfile the LLM created) -> get / list / update / publish / delete, voice
aliasing + unhosted-voice 4xx, single inventory (a directly-seeded native profile
also appears in ``list-agents``), webhook registration (one-time ``webhook_secret``
echo), and the RetellAI ``{status,message}`` error envelope. Shapes flagged
PENDING-FREEZE are pinned against the captured CRM oracle before the contract
freezes (tasks.md gate)."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG
from usan_api.schemas.voice_catalog import VOICE_CATALOG

# A real catalog voice + its derived retell voice_id (voice_map scheme: retell-<FirstName>).
_VOICE = VOICE_CATALOG[0]
_CARTESIA_VOICE = _VOICE.cartesia_voice_id
_RETELL_VOICE = "retell-" + _VOICE.name.split(" - ")[0].split()[0]  # e.g. retell-Sarah


def _create_llm(client, headers, **overrides):
    body = {"start_speaker": "agent", "general_prompt": "You are a helpful assistant."}
    body.update(overrides)
    return client.post("/create-retell-llm", json=body, headers=headers)


def _create_agent(client, headers, llm_id, **overrides):
    body = {
        "response_engine": {"type": "retell-llm", "llm_id": llm_id},
        "voice_id": _RETELL_VOICE,
        "agent_name": "Sales Bot",
    }
    body.update(overrides)
    return client.post("/create-agent", json=body, headers=headers)


def _new_agent(client, headers, **overrides):
    """Full two-step create -> return the created agent JSON."""
    llm = _create_llm(client, headers).json()
    return _create_agent(client, headers, llm["llm_id"], **overrides).json()


@pytest.fixture
def allow_webhook_host(monkeypatch):
    """Populate the compat webhook allow-list so a registration is not fail-closed.
    Runs after the ``client`` fixture's own setenv+cache_clear, so the per-request
    ``get_settings()`` picks the host up."""
    from usan_api.settings import get_settings

    monkeypatch.setenv("COMPAT_WEBHOOK_ALLOWED_HOSTS", "hooks.example.com")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_native_profile(super_async_url: str, org_id: uuid.UUID, name: str) -> uuid.UUID:
    """Insert an AgentProfile the way the admin UI would (NOT via the compat layer, so it
    has no ``compat_extras``) to prove ``list-agents`` is a single inventory."""
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            row = await conn.execute(
                text(
                    "INSERT INTO agent_profiles (organization_id, name, draft_config) "
                    "VALUES (:org, :name, CAST(:cfg AS jsonb)) RETURNING id"
                ),
                {"org": org_id, "name": name, "cfg": json.dumps(DEFAULT_AGENT_CONFIG.model_dump())},
            )
            return row.scalar_one()
    finally:
        await engine.dispose()


# --- create-retell-llm + create-agent (same underlying profile) ------------------------
def test_create_retell_llm_returns_llm_id(compat_client, compat_headers):
    r = _create_llm(compat_client, compat_headers, general_prompt="Be concise.")
    assert r.status_code == 201, r.text
    llm = r.json()
    assert llm["llm_id"].startswith("llm_")
    assert llm["general_prompt"] == "Be concise."  # echoed back


def test_create_agent_binds_to_llm_profile(compat_client, compat_headers):
    llm = _create_llm(compat_client, compat_headers).json()
    r = _create_agent(compat_client, compat_headers, llm["llm_id"])
    assert r.status_code == 201, r.text
    agent = r.json()
    assert agent["agent_id"].startswith("agent_")
    assert agent["agent_name"] == "Sales Bot"
    # agent_id and llm_id are two prefixed views of the SAME AgentProfile (data-model §5).
    assert agent["agent_id"][len("agent_") :] == llm["llm_id"][len("llm_") :]
    assert agent["response_engine"]["llm_id"] == llm["llm_id"]
    assert agent["voice_id"] == _RETELL_VOICE  # echoed in the retell alias form
    assert agent["is_published"] is True  # create-agent publishes -> immediately live
    assert agent["version"] >= 1


def test_create_agent_unknown_llm_returns_4xx(compat_client, compat_headers):
    r = _create_agent(compat_client, compat_headers, "llm_" + "0" * 32)
    assert r.status_code in (404, 422)
    assert r.json()["status"] == r.status_code


def test_create_agent_malformed_llm_id_returns_422(compat_client, compat_headers):
    r = _create_agent(compat_client, compat_headers, "not-a-valid-llm-id")
    assert r.status_code == 422
    assert r.json()["status"] == 422


# --- voice aliasing --------------------------------------------------------------------
def test_create_agent_accepts_raw_cartesia_voice(compat_client, compat_headers):
    llm = _create_llm(compat_client, compat_headers).json()
    r = _create_agent(compat_client, compat_headers, llm["llm_id"], voice_id=_CARTESIA_VOICE)
    assert r.status_code == 201, r.text
    # A raw curated cartesia id is accepted and echoed back in the retell alias form.
    assert r.json()["voice_id"] == _RETELL_VOICE


def test_create_agent_unhosted_voice_returns_documented_4xx(compat_client, compat_headers):
    llm = _create_llm(compat_client, compat_headers).json()
    r = _create_agent(compat_client, compat_headers, llm["llm_id"], voice_id="11labs-NotHosted")
    assert r.status_code == 422  # FR-033 documented error, not an opaque 500
    body = r.json()
    assert body["status"] == 422
    assert "voice" in body["message"].lower()


# --- get / round-trip ------------------------------------------------------------------
def test_get_agent_and_get_retell_llm_same_profile(compat_client, compat_headers):
    agent = _new_agent(compat_client, compat_headers)
    profile_hex = agent["agent_id"][len("agent_") :]

    g = compat_client.get(f"/get-agent/{agent['agent_id']}", headers=compat_headers)
    assert g.status_code == 200
    assert g.json()["agent_id"] == agent["agent_id"]

    llm_id = "llm_" + profile_hex
    gl = compat_client.get(f"/get-retell-llm/{llm_id}", headers=compat_headers)
    assert gl.status_code == 200
    assert gl.json()["llm_id"] == llm_id


def test_get_unknown_agent_returns_404_envelope(compat_client, compat_headers):
    r = compat_client.get("/get-agent/agent_" + "0" * 32, headers=compat_headers)
    assert r.status_code == 404
    assert r.json()["status"] == 404


# --- update (PATCH) -> new version -----------------------------------------------------
def test_update_agent_bumps_version_and_echoes(compat_client, compat_headers):
    agent = _new_agent(compat_client, compat_headers)
    before = agent["version"]
    r = compat_client.patch(
        f"/update-agent/{agent['agent_id']}",
        json={"agent_name": "Renamed Bot"},
        headers=compat_headers,
    )
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["agent_name"] == "Renamed Bot"
    assert updated["version"] > before  # PATCH re-publishes -> a new version


# --- publish-agent-version -------------------------------------------------------------
def test_publish_agent_version(compat_client, compat_headers):
    agent = _new_agent(compat_client, compat_headers)
    before = agent["version"]
    r = compat_client.post(
        f"/publish-agent-version/{agent['agent_id']}",
        json={"version": before, "version_title": "prod"},
        headers=compat_headers,
    )
    assert r.status_code == 200, r.text
    published = r.json()
    assert published["is_published"] is True
    assert published["version"] > before


def test_get_agent_versions_lists_history(compat_client, compat_headers):
    agent = _new_agent(compat_client, compat_headers)
    compat_client.patch(
        f"/update-agent/{agent['agent_id']}", json={"agent_name": "v2"}, headers=compat_headers
    )
    r = compat_client.get(f"/get-agent-versions/{agent['agent_id']}", headers=compat_headers)
    assert r.status_code == 200
    versions = r.json()
    assert isinstance(versions, list)
    assert len(versions) >= 2  # create + update each appended a published version


# --- list-agents (bare array, single inventory) ----------------------------------------
def test_list_agents_is_bare_array_with_api_and_native(
    compat_client, compat_headers, async_database_url
):
    from .conftest import _resolve_usan_org_id

    org_id = asyncio.run(_resolve_usan_org_id(async_database_url))
    asyncio.run(_seed_native_profile(async_database_url, org_id, "Admin UI Agent"))
    agent = _new_agent(compat_client, compat_headers)

    r = compat_client.get("/list-agents", headers=compat_headers)
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, list)  # bare array, NOT {items: [...]}
    names = {a.get("agent_name") for a in data}
    ids = {a["agent_id"] for a in data}
    assert agent["agent_id"] in ids  # the API-created agent
    assert "Admin UI Agent" in names  # the natively-seeded profile (single inventory)
    # Every entry carries the agent_ id shape + a response_engine facade.
    for a in data:
        assert a["agent_id"].startswith("agent_")
        assert a["response_engine"]["llm_id"].startswith("llm_")


def test_list_agents_pagination_walks_full_order_without_drops(compat_client, compat_headers):
    # Paging in small pages, using each page's last agent_id as the next cursor, must reproduce
    # the full unpaginated order EXACTLY — no drops, no duplicates. Regression: the keyset once
    # filtered ``p.id > after`` (raw-UUID order), unrelated to the name sort the list is in, so
    # a second page dropped an arbitrary subset.
    created = {
        _new_agent(compat_client, compat_headers, agent_name=f"Agent {c}")["agent_id"]
        for c in ("D", "B", "A", "C")
    }
    full = [a["agent_id"] for a in compat_client.get("/list-agents", headers=compat_headers).json()]
    assert created <= set(full)

    walked: list[str] = []
    cursor: str | None = None
    for _ in range(len(full) + 2):  # bounded so a buggy cursor can't loop forever
        url = "/list-agents?limit=2" + (f"&pagination_key={cursor}" if cursor else "")
        page = compat_client.get(url, headers=compat_headers).json()
        if not page:
            break
        walked.extend(a["agent_id"] for a in page)
        cursor = page[-1]["agent_id"]
    assert walked == full  # same order, every agent exactly once


# --- delete (archive: gone from the API view) ------------------------------------------
def test_delete_agent_removes_from_api_view(compat_client, compat_headers):
    agent = _new_agent(compat_client, compat_headers)
    d = compat_client.delete(f"/delete-agent/{agent['agent_id']}", headers=compat_headers)
    assert d.status_code == 204
    g = compat_client.get(f"/get-agent/{agent['agent_id']}", headers=compat_headers)
    assert g.status_code == 404
    listed = compat_client.get("/list-agents", headers=compat_headers).json()
    assert agent["agent_id"] not in {a["agent_id"] for a in listed}


# --- retell-llm CRUD + list ------------------------------------------------------------
def test_update_and_list_retell_llms(compat_client, compat_headers):
    llm = _create_llm(compat_client, compat_headers).json()
    _create_agent(compat_client, compat_headers, llm["llm_id"])  # publish the profile

    u = compat_client.patch(
        f"/update-retell-llm/{llm['llm_id']}",
        json={"general_prompt": "Updated prompt."},
        headers=compat_headers,
    )
    assert u.status_code == 200, u.text
    assert u.json()["general_prompt"] == "Updated prompt."

    r = compat_client.get("/list-retell-llms", headers=compat_headers)
    assert r.status_code == 200
    items = r.json()
    assert isinstance(items, list)
    assert llm["llm_id"] in {x["llm_id"] for x in items}


# --- webhook registration (one-time secret echo) ---------------------------------------
def test_create_agent_with_webhook_returns_one_time_secret(
    compat_client, compat_headers, allow_webhook_host
):
    llm = _create_llm(compat_client, compat_headers).json()
    r = _create_agent(
        compat_client,
        compat_headers,
        llm["llm_id"],
        webhook_url="https://hooks.example.com/retell",
        webhook_events=["call_started", "call_ended", "call_analyzed"],
    )
    assert r.status_code == 201, r.text
    agent = r.json()
    # The dedicated per-subscription signing secret is returned ONCE at registration
    # (US2 decision) as a compat-only extra field; a pure-RetellAI CRM ignores it.
    assert agent.get("webhook_secret")
    assert agent["webhook_url"] == "https://hooks.example.com/retell"
    # get-agent must NOT re-expose the secret (never re-readable).
    g = compat_client.get(f"/get-agent/{agent['agent_id']}", headers=compat_headers).json()
    assert "webhook_secret" not in g


def test_create_agent_webhook_off_allow_list_rejected(compat_client, compat_headers):
    # With no COMPAT_WEBHOOK_ALLOWED_HOSTS configured, registration is fail-closed (403):
    # no PHI webhook can ever be registered.
    llm = _create_llm(compat_client, compat_headers).json()
    r = _create_agent(
        compat_client,
        compat_headers,
        llm["llm_id"],
        webhook_url="https://evil.example.org/x",
        webhook_events=["call_ended"],
    )
    assert r.status_code == 403
    assert r.json()["status"] == 403


# --- model containment + name-collision robustness (post-review hardening) --------------
async def _fetch_draft_config(super_async_url: str, profile_id: uuid.UUID) -> dict:
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            row = await conn.execute(
                text("SELECT draft_config FROM agent_profiles WHERE id = :id"),
                {"id": profile_id},
            )
            cfg = row.scalar_one()
            return json.loads(cfg) if isinstance(cfg, str) else cfg
    finally:
        await engine.dispose()


def test_model_temperature_echoed_but_not_honored(
    compat_client, compat_headers, async_database_url
):
    # data-model §5 / Constitution II: model + model_temperature are accepted and echoed but
    # NEVER applied — the prompt runs on the engine's own Vertex pipeline with its own sampling.
    r = _create_llm(compat_client, compat_headers, model="gpt-4o", model_temperature=0.9)
    assert r.status_code == 201, r.text
    llm = r.json()
    assert llm["model_temperature"] == 0.9  # echoed back to the CRM
    profile_id = uuid.UUID(hex=llm["llm_id"][len("llm_") :])
    cfg = asyncio.run(_fetch_draft_config(async_database_url, profile_id))
    # The persisted config keeps the engine default (None), NOT the CRM's 0.9 — not honored.
    default_temp = DEFAULT_AGENT_CONFIG.model_dump()["llm"]["temperature"]
    assert (cfg.get("llm") or {}).get("temperature") == default_temp


def test_duplicate_agent_name_returns_409(
    compat_client, compat_headers, async_database_url, monkeypatch
):
    # Map the residual name-uniqueness race (uq_agent_profiles_name_org) to a clean 409, not a
    # 500. Force the collision by stubbing the dedup to return an already-taken name.
    from usan_api.compat import agent_bridge

    from .conftest import _resolve_usan_org_id

    org_id = asyncio.run(_resolve_usan_org_id(async_database_url))
    asyncio.run(_seed_native_profile(async_database_url, org_id, "Dupe Name"))
    llm = _create_llm(compat_client, compat_headers).json()  # real dedup (unique provisional)

    async def _collide(_db, _base, *, exclude_id=None):  # type: ignore[no-untyped-def]
        return "Dupe Name"

    monkeypatch.setattr(agent_bridge, "_unique_name", _collide)
    r = _create_agent(compat_client, compat_headers, llm["llm_id"], agent_name="Dupe Name")
    assert r.status_code == 409
    assert r.json()["status"] == 409
