# RetellAI Parity Phase 6c — Agent ↔ Conversation-Flow binding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the voice `agent_bridge` serialize and accept the oracle `conversation-flow` response-engine variant (persisted-not-honored), so a RetellAI client with flow-backed agents can create/read/update/list them unchanged.

**Architecture:** A flow binding is stored as a namespaced top-level `compat_response_engine` key in the agent profile's JSONB config (sibling of `compat_extras`, ignored by `AgentConfig`'s `extra="ignore"`). `serialize_agent` derives the `response_engine` `oneOf` variant from that key. `create-agent`/`update-agent` validate the flow in-org (RLS-scoped, cross-org indistinguishable from absent ⇒ 422) and write the key. No migration.

**Tech Stack:** FastAPI compat sub-app, SQLAlchemy 2 async, Pydantic v2, pytest, `openapi-schema-validator` OAS30 + `retell-sdk==5.53.0` conformance.

Spec: `docs/superpowers/specs/2026-07-01-retell-parity-phase6c-agent-flow-binding-design.md`.

## Global Constraints

- **apps/api ONLY.** `services/agent` is untouched. Work from `apps/api/`.
- **No migration.** Alembic single head stays `0049`. Binding rides existing JSONB config.
- **`KNOWN_GAPS` stays `frozenset()`.** No 501 promoted (create/get/update/list-agent were already served). No path drift.
- **Persisted-not-honored.** The bound flow is stored + echoed, NEVER executed. Do not touch the call/worker runtime. Do not block calls on a flow agent.
- **`custom-llm` (or any non-retell/non-flow `type`) ⇒ `CompatError(422, "unsupported response_engine type")`.** We never dial an external LLM websocket.
- **Foreign `llm_id` on update-agent ⇒ `CompatError(409, "cannot bind agent to another agent's llm")`.** Our one-profile overlay can't share an llm across agents.
- **Unknown / malformed / cross-org `conversation_flow_id` ⇒ `CompatError(422, "unknown conversation_flow_id")`** — one message, never acknowledging cross-org existence (mirrors `_validate_kb_ids`).
- **Wire shape:** routers already serialize with `exclude_none=True`; null fields stay omitted. The conversation-flow variant omits `version` when null.
- **Conformance:** the conversation-flow `AgentResponse` must pass `assert_conforms(payload, "AgentResponse")` AND `assert_sdk_roundtrip(payload, "retell.types:AgentResponse")`.
- **`agent_id`/`llm_id` are two views of ONE profile UUID.** A flow agent is `channel='voice'`; retell-llm ops stay channel-agnostic (unchanged).
- **PHI-free audit lines** — do not change the `_audit` calls.
- Commit format: `type(scope): description`, scope `api`. Run `uv run ruff check . && uv run ruff format .` and `uv run mypy` (config `files=["src"]` — never `mypy .`) before each commit. Tests: `uv run pytest` (parallel) or `uv run pytest -n0 <path> -s` for a single test.

---

### Task 1: Read-side — `ResponseEngine.conversation_flow_id` + `_response_engine` variant derivation

**Files:**
- Modify: `apps/api/src/usan_api/compat/schemas/agents.py` (ResponseEngine)
- Modify: `apps/api/src/usan_api/compat/agent_bridge.py` (add `_response_engine`, use it in `serialize_agent`)
- Test: `apps/api/tests/compat/test_agent_response_engine.py` (new — pure unit test, no DB)

**Interfaces:**
- Produces: `agent_bridge._response_engine(profile) -> dict[str, Any]` — returns `{"type":"retell-llm","llm_id":<self>}` when `draft_config["compat_response_engine"]` is absent; returns `{"type":"conversation-flow","conversation_flow_id":<token>[,"version":<int>]}` when present. Consumed by `serialize_agent` (this task) and written by Tasks 2/3.

- [ ] **Step 1: Add `conversation_flow_id` to the ResponseEngine schema**

In `apps/api/src/usan_api/compat/schemas/agents.py`, the `ResponseEngine` class — add the typed field between `llm_id` and `version`:

```python
class ResponseEngine(BaseModel):
    """``response_engine`` on an agent — the Retell-LLM or conversation-flow it speaks through.
    ``llm_id`` decodes to the SAME AgentProfile as the agent (data-model §5);
    ``conversation_flow_id`` decodes to a separate ``conversation_flows`` row (Phase 6a)."""

    model_config = ConfigDict(extra="allow")

    type: str = "retell-llm"
    llm_id: str | None = None
    conversation_flow_id: str | None = None
    version: int | None = None
```

- [ ] **Step 2: Write the failing unit test**

Create `apps/api/tests/compat/test_agent_response_engine.py`:

```python
"""Unit tests for agent_bridge._response_engine variant derivation (Phase 6c)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from usan_api.compat import agent_bridge, ids


def _profile(config: dict) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), draft_config=config)


def test_response_engine_defaults_to_retell_llm_self_view():
    p = _profile({"prompts": {}, "voice": {}})
    eng = agent_bridge._response_engine(p)
    assert eng == {"type": "retell-llm", "llm_id": ids.encode_llm_id(p.id)}


def test_response_engine_conversation_flow_with_version():
    token = ids.encode_conversation_flow_id(uuid.uuid4())
    p = _profile(
        {"compat_response_engine": {"type": "conversation-flow",
                                    "conversation_flow_id": token, "version": 3}}
    )
    assert agent_bridge._response_engine(p) == {
        "type": "conversation-flow", "conversation_flow_id": token, "version": 3
    }


def test_response_engine_conversation_flow_omits_null_version():
    token = ids.encode_conversation_flow_id(uuid.uuid4())
    p = _profile(
        {"compat_response_engine": {"type": "conversation-flow",
                                    "conversation_flow_id": token, "version": None}}
    )
    eng = agent_bridge._response_engine(p)
    assert eng == {"type": "conversation-flow", "conversation_flow_id": token}
    assert "version" not in eng


def test_response_engine_ignores_none_draft_config():
    p = SimpleNamespace(id=uuid.uuid4(), draft_config=None)
    assert agent_bridge._response_engine(p)["type"] == "retell-llm"
```

- [ ] **Step 3: Run it to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_agent_response_engine.py -q`
Expected: FAIL — `AttributeError: module 'usan_api.compat.agent_bridge' has no attribute '_response_engine'`.

- [ ] **Step 4: Add `_response_engine` and use it in `serialize_agent`**

In `apps/api/src/usan_api/compat/agent_bridge.py`, add the helper immediately above `serialize_agent` (after `serialize_agent_version`):

```python
def _response_engine(profile: AgentProfile) -> dict[str, Any]:
    """Derive the oracle ``response_engine`` oneOf variant from stored state.

    A flow-bound agent stores ``compat_response_engine`` (Phase 6c); absent ⇒ the
    retell-llm self-view (``llm_id`` == this profile, data-model §5). ``version`` is
    omitted when null (oracle omit-nulls)."""
    stored = (profile.draft_config or {}).get("compat_response_engine")
    if stored and stored.get("type") == "conversation-flow":
        engine: dict[str, Any] = {
            "type": "conversation-flow",
            "conversation_flow_id": stored["conversation_flow_id"],
        }
        if stored.get("version") is not None:
            engine["version"] = stored["version"]
        return engine
    return {"type": "retell-llm", "llm_id": ids.encode_llm_id(profile.id)}
```

Then in `serialize_agent`, replace the hard-coded response_engine line inside the `data.update({...})` call:

```python
    data.update(
        {
            "agent_id": ids.encode_agent_id(profile.id),
            "agent_name": profile.name,
            "response_engine": _response_engine(profile),
            "voice_id": voice_map.to_retell_voice_id(cartesia),
            **_version_fields(profile),
        }
    )
```

(Only the `"response_engine"` value changes — from the literal dict to `_response_engine(profile)`.)

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_agent_response_engine.py -q`
Expected: PASS (4 passed).

- [ ] **Step 6: Regression — the existing agent suites still pass**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_freeze_agents.py tests/compat/test_agent_bridge.py -q`
Expected: PASS (retell-llm agents unchanged — `_response_engine` returns the same self-view).

- [ ] **Step 7: Lint + type-check + commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/compat/schemas/agents.py src/usan_api/compat/agent_bridge.py tests/compat/test_agent_response_engine.py
git commit -m "feat(api): Phase 6c — serialize_agent derives response_engine variant"
```

---

### Task 2: create-agent — accept a `conversation-flow` engine

**Files:**
- Modify: `apps/api/src/usan_api/compat/agent_bridge.py` (imports, `_provisional_agent_name`, `_validate_flow_id`, `_store_flow_engine`, `_clear_flow_engine`, `_create_flow_agent`, branch in `bind_agent`)
- Test: `apps/api/tests/compat/test_agent_flow_binding.py` (new)
- Test: `apps/api/tests/compat/test_freeze_agents.py` (add conversation-flow freeze)

**Interfaces:**
- Consumes: `_response_engine`/`serialize_agent` (Task 1).
- Produces: `_validate_flow_id(db, token) -> uuid.UUID`, `_store_flow_engine(config, *, flow_uuid, version)`, `_clear_flow_engine(config)` — reused by Task 3.

- [ ] **Step 1: Write the failing end-to-end tests**

Create `apps/api/tests/compat/test_agent_flow_binding.py`:

```python
"""create/update-agent conversation-flow binding fidelity (Phase 6c)."""

from __future__ import annotations

import uuid

import pytest

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip
from tests.compat.conftest import RETELL_VOICE
from usan_api.compat import ids

_FLOW = {
    "start_speaker": "agent",
    "model_choice": {"type": "cascading", "model": "gpt-4.1"},
    "nodes": [],
}


def _create_flow(compat_client, compat_headers) -> str:
    r = compat_client.post("/create-conversation-flow", json=_FLOW, headers=compat_headers)
    assert r.status_code == 201, r.text
    return r.json()["conversation_flow_id"]


def _create_flow_agent(compat_client, compat_headers, flow_id, **extra):
    body = {
        "response_engine": {"type": "conversation-flow", "conversation_flow_id": flow_id},
        "voice_id": RETELL_VOICE,
        "agent_name": "Flow Bot",
        **extra,
    }
    return compat_client.post("/create-agent", json=body, headers=compat_headers)


def test_create_conversation_flow_agent(compat_client, compat_headers):
    flow_id = _create_flow(compat_client, compat_headers)
    r = _create_flow_agent(compat_client, compat_headers, flow_id)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["response_engine"] == {
        "type": "conversation-flow", "conversation_flow_id": flow_id
    }
    assert_conforms(body, "AgentResponse")
    assert_sdk_roundtrip(body, "retell.types:AgentResponse")


def test_get_and_list_echo_flow_variant(compat_client, compat_headers):
    flow_id = _create_flow(compat_client, compat_headers)
    agent_id = _create_flow_agent(compat_client, compat_headers, flow_id).json()["agent_id"]
    got = compat_client.get(f"/get-agent/{agent_id}", headers=compat_headers).json()
    assert got["response_engine"]["type"] == "conversation-flow"
    assert got["response_engine"]["conversation_flow_id"] == flow_id
    listed = compat_client.get("/list-agents", headers=compat_headers).json()
    match = [a for a in listed if a["agent_id"] == agent_id]
    assert match and match[0]["response_engine"]["conversation_flow_id"] == flow_id


def test_create_flow_agent_echoes_version(compat_client, compat_headers):
    flow_id = _create_flow(compat_client, compat_headers)
    r = _create_flow_agent(
        compat_client, compat_headers, flow_id,
        response_engine={"type": "conversation-flow",
                         "conversation_flow_id": flow_id, "version": 2},
    )
    assert r.status_code == 201, r.text
    assert r.json()["response_engine"]["version"] == 2


def test_create_flow_agent_missing_flow_id_is_422(compat_client, compat_headers):
    r = compat_client.post(
        "/create-agent",
        json={"response_engine": {"type": "conversation-flow"}, "voice_id": RETELL_VOICE},
        headers=compat_headers,
    )
    assert r.status_code == 422, r.text


def test_create_flow_agent_unknown_flow_is_422(compat_client, compat_headers):
    bogus = ids.encode_conversation_flow_id(uuid.uuid4())
    r = _create_flow_agent(compat_client, compat_headers, bogus)
    assert r.status_code == 422, r.text


def test_create_flow_agent_malformed_flow_is_422(compat_client, compat_headers):
    r = _create_flow_agent(compat_client, compat_headers, "not-a-flow-id")
    assert r.status_code == 422, r.text


def test_create_custom_llm_agent_is_422(compat_client, compat_headers):
    r = compat_client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "custom-llm",
                                "llm_websocket_url": "wss://evil.example/llm"},
            "voice_id": RETELL_VOICE,
        },
        headers=compat_headers,
    )
    assert r.status_code == 422, r.text


def test_create_retell_llm_agent_still_works(compat_client, compat_headers):
    llm = compat_client.post(
        "/create-retell-llm",
        json={"start_speaker": "agent", "general_prompt": "hi"},
        headers=compat_headers,
    ).json()
    r = compat_client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
            "voice_id": RETELL_VOICE,
        },
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    assert r.json()["response_engine"] == {"type": "retell-llm", "llm_id": llm["llm_id"]}
```

- [ ] **Step 2: Run to verify failures**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_agent_flow_binding.py -q`
Expected: FAIL — the conversation-flow create returns 422 today (`bind_agent` requires `llm_id`); the custom-llm/retell-llm cases may pass incidentally.

- [ ] **Step 3: Add the import for the flows repo**

In `apps/api/src/usan_api/compat/agent_bridge.py`, alongside the other repo imports (after `from usan_api.repositories import compat_webhooks as compat_webhooks_repo`):

```python
from usan_api.repositories import conversation_flows as conversation_flows_repo
```

- [ ] **Step 4: Add the flow helpers**

In `agent_bridge.py`, add just after `_provisional_llm_name`:

```python
def _provisional_agent_name() -> str:
    return f"agent-{uuid.uuid4().hex[:8]}"


async def _validate_flow_id(db: AsyncSession, token: str) -> uuid.UUID:
    """Reject a conversation_flow_id that doesn't resolve within the caller's org (RLS).
    Cross-org is indistinguishable from absent — a single 422 that never acknowledges
    cross-org existence (mirrors ``_validate_kb_ids``)."""
    try:
        flow_uuid = ids.decode_conversation_flow_id(token)
    except CompatError as exc:
        raise CompatError(422, "unknown conversation_flow_id") from exc
    flow = await conversation_flows_repo.get(db, flow_uuid)
    if flow is None:
        raise CompatError(422, "unknown conversation_flow_id")
    return flow_uuid


def _store_flow_engine(
    config: dict[str, Any], *, flow_uuid: uuid.UUID, version: int | None
) -> None:
    """Persist the conversation-flow binding as a namespaced top-level config key. The
    canonical re-encoded token keeps the echo stable; ``version`` is accept-and-echo."""
    engine: dict[str, Any] = {
        "type": "conversation-flow",
        "conversation_flow_id": ids.encode_conversation_flow_id(flow_uuid),
    }
    if version is not None:
        engine["version"] = version
    config["compat_response_engine"] = engine


def _clear_flow_engine(config: dict[str, Any]) -> None:
    """Revert to the retell-llm self-view (used when update-agent re-binds to retell-llm)."""
    config.pop("compat_response_engine", None)
```

- [ ] **Step 5: Add `_create_flow_agent` and branch `bind_agent`**

In `agent_bridge.py`, add `_create_flow_agent` immediately above `bind_agent`:

```python
async def _create_flow_agent(
    db: AsyncSession, settings: Settings, body: CreateAgentRequest
) -> tuple[AgentProfile, str | None]:
    """create-agent with a conversation-flow engine: mint a FRESH voice profile bound to an
    in-org flow, then publish. (Unlike retell-llm, there is no prior create-retell-llm step —
    the flow already exists in the Phase-6a table.)"""
    engine = body.response_engine
    if not engine.conversation_flow_id:
        raise CompatError(422, "response_engine.conversation_flow_id is required")
    flow_uuid = await _validate_flow_id(db, engine.conversation_flow_id)
    cartesia = voice_map.resolve_voice_id(body.voice_id)

    config = DEFAULT_AGENT_CONFIG.model_dump()
    _apply_voice_overlay(config, cartesia_voice_id=cartesia)
    _store_flow_engine(config, flow_uuid=flow_uuid, version=engine.version)
    _merge_extras(config, "agent", body.model_dump())
    _validate_config(config)

    profile = await agent_profiles_repo.create_profile(
        db, name=_provisional_agent_name(), description=None, actor_email=_ACTOR
    )
    secret: str | None = None
    if body.webhook_url is not None:
        secret = await _register_webhook(
            db, settings, profile.id, body.webhook_url, body.webhook_events
        )
    updated = await agent_profiles_repo.update_draft(
        db, profile.id, config=config, description=None, actor_email=_ACTOR
    )
    if updated is None:  # pragma: no cover - just created above
        raise CompatError(404, "agent not found")
    updated.channel = "voice"
    if body.agent_name:
        updated.name = await _unique_name(db, body.agent_name, exclude_id=updated.id)
    await _publish_and_commit(db, profile.id, note="compat create-agent (conversation-flow)")
    await db.refresh(updated)
    return updated, secret
```

Then at the TOP of `bind_agent`, replace the opening `llm_id = body.response_engine.llm_id` lines with the branch (keep everything from `profile = await _load_active(...)` onward UNCHANGED):

```python
async def bind_agent(
    db: AsyncSession, settings: Settings, body: CreateAgentRequest
) -> tuple[AgentProfile, str | None]:
    """create-agent: bind the agent half onto the profile its ``response_engine.llm_id``
    points at, then publish (immediately live). Returns (profile, one-time webhook secret).
    A ``conversation-flow`` engine takes the fresh-profile path instead (Phase 6c)."""
    engine = body.response_engine
    if engine.type == "conversation-flow":
        return await _create_flow_agent(db, settings, body)
    if engine.type != "retell-llm":
        raise CompatError(422, "unsupported response_engine type")
    llm_id = engine.llm_id
    if not llm_id:
        raise CompatError(422, "response_engine.llm_id is required")
    profile = await _load_active(db, ids.decode_llm_id(llm_id), kind="response engine")
    # ... rest of the existing bind_agent body UNCHANGED ...
```

- [ ] **Step 6: Run the binding tests**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_agent_flow_binding.py -q`
Expected: PASS (all create-side tests).

- [ ] **Step 7: Add the conversation-flow freeze test**

Change the import line at the top of `apps/api/tests/compat/test_freeze_agents.py` to include the sdk helper:

```python
from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip
```

Then append:

```python
def test_conversation_flow_agent_conforms_to_oracle(compat_client, compat_headers):
    """A conversation-flow-bound agent response conforms to the oracle + retell-sdk."""
    flow = compat_client.post(
        "/create-conversation-flow",
        json={"start_speaker": "agent",
              "model_choice": {"type": "cascading", "model": "gpt-4.1"}, "nodes": []},
        headers=compat_headers,
    ).json()
    r = compat_client.post(
        "/create-agent",
        json={
            "response_engine": {"type": "conversation-flow",
                                "conversation_flow_id": flow["conversation_flow_id"]},
            "voice_id": RETELL_VOICE,
            "agent_name": "Flow Bot",
        },
        headers=compat_headers,
    )
    assert r.status_code == 201, r.text
    assert_conforms(r.json(), "AgentResponse")
    assert_sdk_roundtrip(r.json(), "retell.types:AgentResponse")
```

- [ ] **Step 8: Run the freeze suite**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_freeze_agents.py -q`
Expected: PASS.

- [ ] **Step 9: Lint + type-check + commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/compat/agent_bridge.py tests/compat/test_agent_flow_binding.py tests/compat/test_freeze_agents.py
git commit -m "feat(api): Phase 6c — create-agent accepts conversation-flow engine"
```

---

### Task 3: update-agent — re-point / switch / revert the response_engine

**Files:**
- Modify: `apps/api/src/usan_api/compat/agent_bridge.py` (`update_agent`)
- Test: `apps/api/tests/compat/test_agent_flow_binding.py` (append update cases)

**Interfaces:**
- Consumes: `_validate_flow_id`, `_store_flow_engine`, `_clear_flow_engine` (Task 2).

- [ ] **Step 1: Write the failing update tests**

Append to `apps/api/tests/compat/test_agent_flow_binding.py`:

```python
def _create_retell_agent(compat_client, compat_headers) -> tuple[str, str]:
    llm = compat_client.post(
        "/create-retell-llm",
        json={"start_speaker": "agent", "general_prompt": "hi"},
        headers=compat_headers,
    ).json()
    agent = compat_client.post(
        "/create-agent",
        json={"response_engine": {"type": "retell-llm", "llm_id": llm["llm_id"]},
              "voice_id": RETELL_VOICE},
        headers=compat_headers,
    ).json()
    return agent["agent_id"], llm["llm_id"]


def test_update_switches_llm_agent_to_flow(compat_client, compat_headers):
    agent_id, _ = _create_retell_agent(compat_client, compat_headers)
    flow_id = _create_flow(compat_client, compat_headers)
    r = compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"response_engine": {"type": "conversation-flow",
                                  "conversation_flow_id": flow_id}},
        headers=compat_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["response_engine"] == {
        "type": "conversation-flow", "conversation_flow_id": flow_id
    }
    assert_conforms(r.json(), "AgentResponse")


def test_update_repoints_flow_agent_to_another_flow(compat_client, compat_headers):
    flow_a = _create_flow(compat_client, compat_headers)
    flow_b = _create_flow(compat_client, compat_headers)
    agent_id = _create_flow_agent(compat_client, compat_headers, flow_a).json()["agent_id"]
    r = compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"response_engine": {"type": "conversation-flow",
                                  "conversation_flow_id": flow_b}},
        headers=compat_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["response_engine"]["conversation_flow_id"] == flow_b


def test_update_reverts_flow_agent_to_self_llm(compat_client, compat_headers):
    agent_id, llm_id = _create_retell_agent(compat_client, compat_headers)
    flow_id = _create_flow(compat_client, compat_headers)
    compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"response_engine": {"type": "conversation-flow",
                                  "conversation_flow_id": flow_id}},
        headers=compat_headers,
    )
    r = compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"response_engine": {"type": "retell-llm", "llm_id": llm_id}},
        headers=compat_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["response_engine"] == {"type": "retell-llm", "llm_id": llm_id}


def test_update_foreign_llm_id_is_409(compat_client, compat_headers):
    agent_a, _ = _create_retell_agent(compat_client, compat_headers)
    _, llm_b = _create_retell_agent(compat_client, compat_headers)
    r = compat_client.patch(
        f"/update-agent/{agent_a}",
        json={"response_engine": {"type": "retell-llm", "llm_id": llm_b}},
        headers=compat_headers,
    )
    assert r.status_code == 409, r.text


def test_update_unknown_flow_is_422(compat_client, compat_headers):
    agent_id, _ = _create_retell_agent(compat_client, compat_headers)
    bogus = ids.encode_conversation_flow_id(uuid.uuid4())
    r = compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"response_engine": {"type": "conversation-flow",
                                  "conversation_flow_id": bogus}},
        headers=compat_headers,
    )
    assert r.status_code == 422, r.text


def test_update_custom_llm_is_422(compat_client, compat_headers):
    agent_id, _ = _create_retell_agent(compat_client, compat_headers)
    r = compat_client.patch(
        f"/update-agent/{agent_id}",
        json={"response_engine": {"type": "custom-llm",
                                  "llm_websocket_url": "wss://x/y"}},
        headers=compat_headers,
    )
    assert r.status_code == 422, r.text
```

- [ ] **Step 2: Run to verify failures**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_agent_flow_binding.py -k update -q`
Expected: FAIL — update-agent ignores `response_engine` today, so the switch/revert echo the wrong variant and the 409/422 cases return 200.

- [ ] **Step 3: Handle `response_engine` in `update_agent`**

In `agent_bridge.py`, inside `update_agent`, after the voice-overlay `if body.voice_id is not None:` block and BEFORE the `_merge_extras(...)` call, insert:

```python
    if body.response_engine is not None:
        engine = body.response_engine
        if engine.type == "conversation-flow":
            if not engine.conversation_flow_id:
                raise CompatError(422, "response_engine.conversation_flow_id is required")
            flow_uuid = await _validate_flow_id(db, engine.conversation_flow_id)
            _store_flow_engine(config, flow_uuid=flow_uuid, version=engine.version)
        elif engine.type == "retell-llm":
            if not engine.llm_id:
                raise CompatError(422, "response_engine.llm_id is required")
            if engine.llm_id != ids.encode_llm_id(profile.id):
                # Our one-profile overlay can't represent RetellAI's one-llm-many-agents.
                raise CompatError(409, "cannot bind agent to another agent's llm")
            _clear_flow_engine(config)
        else:
            raise CompatError(422, "unsupported response_engine type")
```

The surrounding `update_agent` is otherwise unchanged: `config` is already the deep copy from `_config_dict(profile)`, and the existing `_validate_config(config)` + `update_draft` + `_publish_and_commit` persist the mutated key in the same publish/commit.

- [ ] **Step 4: Run the update tests**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_agent_flow_binding.py -q`
Expected: PASS (all create + update cases).

- [ ] **Step 5: Lint + type-check + commit**

```bash
cd apps/api && uv run ruff check . && uv run ruff format . && uv run mypy
git add src/usan_api/compat/agent_bridge.py tests/compat/test_agent_flow_binding.py
git commit -m "feat(api): Phase 6c — update-agent re-points/switches response_engine"
```

---

### Task 4: Operator note

**Files:**
- Create: `docs/deployment/agent-conversation-flow-binding.md`

- [ ] **Step 1: Write the operator note**

Create `docs/deployment/agent-conversation-flow-binding.md`:

```markdown
# Agent ↔ Conversation-Flow binding (Phase 6c)

**Status:** merged, INERT until the next `v*` tag deploy (merged ≠ deployed). No new env
keys, no migration (single alembic head stays `0049`).

## What shipped

`create-agent` / `update-agent` now accept the oracle `response_engine` variant
`{type: "conversation-flow", conversation_flow_id, version?}` (in addition to `retell-llm`).
`get-agent` / `list-agents` / `get-agent-versions` echo the bound variant.

The binding is stored as a `compat_response_engine` key in the agent profile's JSONB config
(no schema change) and validated against the org's own `conversation_flows` rows (Phase 6a,
RLS-scoped). A cross-org / unknown / malformed flow id returns `422 "unknown
conversation_flow_id"` (never acknowledging cross-org existence).

## Persisted-not-honored (IMPORTANT)

The bound conversation flow is **echoed but NOT executed.** A conversation-flow agent runs
the engine's default Vertex pipeline against its (empty/default) prompt config — it does
**not** run the flow DAG. Honoring the DAG at call time is the later **6-runtime** phase.
Calls to a flow agent are **not blocked** (accept-and-persist posture, matching phone-number
bindings and `current_node_id`). Do not point production traffic at a flow-only agent
expecting flow behavior until 6-runtime ships.

## Not covered

- `custom-llm` engines are rejected (`422 "unsupported response_engine type"`) — we never
  dial an external LLM websocket (PHI containment).
- **Chat-agent** flow binding (`chat_agent_bridge`) is a deferred **6c-chat** follow-up.
- `update-agent` reverting a flow agent to `retell-llm` requires the agent's OWN `llm_id`
  (self); a foreign `llm_id` returns `409` (one-profile overlay can't share an llm).
```

- [ ] **Step 2: Commit**

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine
git add docs/deployment/agent-conversation-flow-binding.md
git commit -m "docs(api): Phase 6c operator note — agent conversation-flow binding"
```

---

## Final verification (after all tasks)

- [ ] Full apps/api suite green: `cd apps/api && uv run pytest -q` (expect the pre-existing count + the new 6c tests; ignore known -n auto testcontainer flakes — re-run in isolation to confirm).
- [ ] `uv run ruff check . && uv run mypy` clean.
- [ ] Single alembic head: `uv run alembic heads` → `0049` only.
- [ ] `KNOWN_GAPS` unchanged (`tests/compat/test_surface_coverage.py` + `tests/test_compat_fidelity.py` both green — no 501 moved).
- [ ] Whole-branch review (opus), then the independent `/review`, then `finishing-a-development-branch`.
