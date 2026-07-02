# Phase 7 slice 1 — `agent-playground-completion` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve `POST /agent-playground-completion/{agent_id}` as a LIVE, stateless single-turn Vertex completion conformant to the pinned RetellAI oracle, promoted out of the 501 router.

**Architecture:** A new dedicated compat router (`compat/routers/playground.py`) delegates to a thin service (`compat/playground_service.py`) that resolves the org-scoped published `AgentConfig`, builds a system prompt from `cfg.prompts.system_prompt` + dynamic vars, maps the request `messages` to genai `contents`, and runs one `run_vertex_turn(tools=[])`. Returns one agent message; every unproduced optional field is omitted via `exclude_none`. No migration, no new env key, no `services/agent` involvement.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy async, Google GenAI (Vertex via `run_vertex_turn`, ADC), pytest (`-n auto`, `asyncio_mode=auto`).

**Design spec:** `docs/superpowers/specs/2026-07-01-retell-parity-phase7-playground-design.md`.

## Global Constraints

- **Constitution I:** `apps/api` and `services/agent` never import each other — trivially held (no agent touch).
- **Constitution II (PHI containment):** Vertex only via `run_vertex_turn` (`vertexai=True`, ADC — never Gemini Dev API). Logs `type(exc).__name__` + counts only; never message content, dynamic vars, or the instruction. Nothing persisted.
- **Oracle governs:** response conforms to `retell.types:PlaygroundCompletionResponse` (SDK round-trip) + oracle `MessageOrToolCall`; `exclude_none=True` omits every optional field when null (RetellAI omits nulls).
- **Errors (this op declares 400/401/402/422/429/500 — NO 404):** malformed id → `CompatError(422, "invalid agent_id")`; unknown/cross-org/unpublished agent → `CompatError(422, "agent is not available")`; `gcp_project` unset → `CompatError(503, "playground completion unavailable")`; any other exception in the Vertex path → `CompatError(502, "playground completion failed") from None`.
- **`?version` accepted-and-ignored** (serve currently-published), matching `get-chat-agent`.
- **Ships inert:** no migration, no new env key. Gated by existing compat-key auth (401 until key minted) + `gcp_project` 503. Not deployed until a `v*` tag. `KNOWN_GAPS` stays `frozenset()`.
- **ruff line-length 100, target py314; `uv run mypy` (config `files=["src"]`) — never `mypy .`.**
- **Both** surface tests updated when the op moves 501→served: `tests/compat/test_surface_coverage.py` (data-driven, no edit needed beyond removing the `_UNSUPPORTED` entry) and `tests/test_compat_fidelity.py` (remove the parametrize entry).

---

### Task 1: Playground request/response schemas

**Files:**
- Create: `apps/api/src/usan_api/compat/schemas/playground.py`
- Test: `apps/api/tests/compat/test_playground_schemas.py`

**Interfaces:**
- Consumes: nothing (pure Pydantic).
- Produces:
  - `PlaygroundMessageInput` — `role: str`, `content: str | None = None`, `message_id: str | None = None`, `model_config = ConfigDict(extra="allow")`.
  - `PlaygroundCompletionRequest` — `messages: list[PlaygroundMessageInput]` (validator: non-empty), `dynamic_variables: dict[str, str] | None = None`, `tool_mocks: list[Any] | None = None`, `current_state: str | None = None`, `current_node_id: str | None = None`, `component_id: str | None = None`, `model_config = ConfigDict(extra="allow")`.
  - `PlaygroundMessageOut` — `message_id: str`, `role: Literal["agent"] = "agent"`, `content: str`, `created_timestamp: int`.
  - `PlaygroundCompletionResponse` — `messages: list[PlaygroundMessageOut]`, plus `current_state / current_node_id: str | None = None`, `dynamic_variables: dict[str, str] | None = None`, `call_ended: bool | None = None`, `knowledge_base_retrieved_contents: list[str] | None = None`.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/compat/test_playground_schemas.py`:

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from usan_api.compat.schemas.playground import (
    PlaygroundCompletionRequest,
    PlaygroundCompletionResponse,
    PlaygroundMessageOut,
)


def test_request_requires_non_empty_messages() -> None:
    with pytest.raises(ValidationError):
        PlaygroundCompletionRequest(messages=[])


def test_request_accepts_advanced_fields_and_extra_keys() -> None:
    req = PlaygroundCompletionRequest.model_validate(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "dynamic_variables": {"name": "Jo"},
            "tool_mocks": [{"tool_name": "x", "output": "y", "input_match_rule": "any"}],
            "current_state": "greeting",
            "current_node_id": "node_1",
            "component_id": "comp_1",
            "some_future_key": True,
        }
    )
    assert req.messages[0].role == "user"
    assert req.messages[0].content == "hi"
    assert req.dynamic_variables == {"name": "Jo"}


def test_request_tolerates_content_less_variant() -> None:
    # a tool-call ChatMessageInput variant carries no `content` — must not error
    req = PlaygroundCompletionRequest.model_validate(
        {"messages": [{"role": "tool_call_invocation", "tool_call_id": "t1"}]}
    )
    assert req.messages[0].content is None


def test_response_exclude_none_omits_unproduced_fields() -> None:
    resp = PlaygroundCompletionResponse(
        messages=[PlaygroundMessageOut(message_id="m1", content="hello", created_timestamp=1)]
    )
    dumped = resp.model_dump(exclude_none=True)
    assert dumped == {
        "messages": [
            {"message_id": "m1", "role": "agent", "content": "hello", "created_timestamp": 1}
        ]
    }
    for k in (
        "current_state",
        "current_node_id",
        "dynamic_variables",
        "call_ended",
        "knowledge_base_retrieved_contents",
    ):
        assert k not in dumped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_playground_schemas.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'usan_api.compat.schemas.playground'`.

- [ ] **Step 3: Write minimal implementation**

Create `apps/api/src/usan_api/compat/schemas/playground.py`:

```python
"""Schemas for the RetellAI-compat agent-playground-completion op (Phase 7 slice 1).

A stateless single-turn completion. Advanced request fields (tool_mocks,
current_state, current_node_id, component_id) are accepted for forward-compat but
not acted on this slice; response optional fields default None and are omitted via
model_dump(exclude_none=True). See
docs/superpowers/specs/2026-07-01-retell-parity-phase7-playground-design.md.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator


class PlaygroundMessageInput(BaseModel):
    """One input message. `role`/`content` cover the MessageBase variant; the other
    ChatMessageInput oneOf variants (tool-call / transition / injected / sms) are
    tolerated via extra='allow' and skipped when they carry no text `content`."""

    model_config = ConfigDict(extra="allow")

    role: str
    content: str | None = None
    message_id: str | None = None


class PlaygroundCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    messages: list[PlaygroundMessageInput]
    dynamic_variables: dict[str, str] | None = None
    tool_mocks: list[Any] | None = None
    current_state: str | None = None
    current_node_id: str | None = None
    component_id: str | None = None

    @field_validator("messages")
    @classmethod
    def _messages_non_empty(cls, v: list[PlaygroundMessageInput]) -> list[PlaygroundMessageInput]:
        if not v:
            raise ValueError("messages must not be empty")
        return v


class PlaygroundMessageOut(BaseModel):
    message_id: str
    role: Literal["agent"] = "agent"
    content: str
    created_timestamp: int


class PlaygroundCompletionResponse(BaseModel):
    messages: list[PlaygroundMessageOut]
    current_state: str | None = None
    current_node_id: str | None = None
    dynamic_variables: dict[str, str] | None = None
    call_ended: bool | None = None
    knowledge_base_retrieved_contents: list[str] | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_playground_schemas.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/compat/schemas/playground.py apps/api/tests/compat/test_playground_schemas.py
git commit -m "feat(api): playground-completion request/response schemas"
```

---

### Task 2: `run_playground_completion` service

**Files:**
- Create: `apps/api/src/usan_api/compat/playground_service.py`
- Test: `apps/api/tests/compat/test_playground_service.py`

**Interfaces:**
- Consumes: `PlaygroundCompletionRequest`, `PlaygroundCompletionResponse`, `PlaygroundMessageOut` (Task 1); `ids.decode_agent_id`, `agent_profiles_repo.get_profile` / `get_published_config`, `AgentConfig.model_validate`, `build_vars`, `substitute`, `run_vertex_turn`, `CompatError`, `Settings`.
- Produces: `async def run_playground_completion(db: AsyncSession, settings: Settings, *, agent_id: str, version: str | None, request: PlaygroundCompletionRequest) -> PlaygroundCompletionResponse`.

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/compat/test_playground_service.py`:

```python
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from usan_api.compat.errors import CompatError
from usan_api.compat.ids import encode_agent_id
from usan_api.compat.playground_service import run_playground_completion
from usan_api.compat.schemas.playground import PlaygroundCompletionRequest
from usan_api.models.agent_profile import AgentProfile, AgentProfileVersion, ProfileStatus
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context
from usan_api.vertex_test import VertexTurn

_CONFIG = DEFAULT_AGENT_CONFIG.model_copy(
    update={
        "prompts": DEFAULT_AGENT_CONFIG.prompts.model_copy(
            update={"system_prompt": "You are {{name}}. Be brief."}
        )
    }
).model_dump()


async def _seed_published_profile(db) -> AgentProfile:
    profile = AgentProfile(
        name=f"PG Agent {uuid.uuid4().hex[:8]}",
        draft_config=_CONFIG,
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    db.add(profile)
    await db.flush()
    db.add(AgentProfileVersion(profile_id=profile.id, version=1, config=_CONFIG))
    await db.flush()
    return profile


def _settings(project: str | None):
    return get_settings().model_copy(update={"gcp_project": project})


async def _current_org(db) -> uuid.UUID:
    return (await db.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()


async def test_happy_path_single_turn(app_session, monkeypatch) -> None:
    org = await _current_org(app_session)
    await set_tenant_context(app_session, org)
    profile = await _seed_published_profile(app_session)

    captured: dict = {}

    async def fake_turn(**kwargs):
        captured.update(kwargs)
        return VertexTurn(text="Hello!")

    monkeypatch.setattr(
        "usan_api.compat.playground_service.run_vertex_turn", fake_turn
    )

    req = PlaygroundCompletionRequest(messages=[{"role": "user", "content": "hi"}])
    resp = await run_playground_completion(
        app_session, _settings("proj"), agent_id=encode_agent_id(profile.id), version=None, request=req
    )
    assert len(resp.messages) == 1
    assert resp.messages[0].role == "agent"
    assert resp.messages[0].content == "Hello!"
    assert resp.messages[0].message_id
    assert resp.messages[0].created_timestamp > 0
    # tools always empty; last content is the user turn
    assert captured["tools"] == []
    assert captured["contents"][-1] == {"role": "user", "parts": [{"text": "hi"}]}


async def test_multi_turn_role_mapping(app_session, monkeypatch) -> None:
    org = await _current_org(app_session)
    await set_tenant_context(app_session, org)
    profile = await _seed_published_profile(app_session)
    captured: dict = {}

    async def fake_turn(**kwargs):
        captured.update(kwargs)
        return VertexTurn(text="ok")

    monkeypatch.setattr("usan_api.compat.playground_service.run_vertex_turn", fake_turn)
    req = PlaygroundCompletionRequest(
        messages=[
            {"role": "user", "content": "one"},
            {"role": "agent", "content": "two"},
            {"role": "user", "content": "three"},
        ]
    )
    await run_playground_completion(
        app_session, _settings("proj"), agent_id=encode_agent_id(profile.id), version=None, request=req
    )
    assert [c["role"] for c in captured["contents"]] == ["user", "model", "user"]


async def test_dynamic_variables_substituted(app_session, monkeypatch) -> None:
    org = await _current_org(app_session)
    await set_tenant_context(app_session, org)
    profile = await _seed_published_profile(app_session)
    captured: dict = {}

    async def fake_turn(**kwargs):
        captured.update(kwargs)
        return VertexTurn(text="ok")

    monkeypatch.setattr("usan_api.compat.playground_service.run_vertex_turn", fake_turn)
    req = PlaygroundCompletionRequest(
        messages=[{"role": "user", "content": "hi"}], dynamic_variables={"name": "Robo"}
    )
    await run_playground_completion(
        app_session, _settings("proj"), agent_id=encode_agent_id(profile.id), version=None, request=req
    )
    assert "Robo" in captured["system_instruction"]


async def test_advanced_fields_ignored(app_session, monkeypatch) -> None:
    org = await _current_org(app_session)
    await set_tenant_context(app_session, org)
    profile = await _seed_published_profile(app_session)

    async def fake_turn(**kwargs):
        return VertexTurn(text="only one")

    monkeypatch.setattr("usan_api.compat.playground_service.run_vertex_turn", fake_turn)
    req = PlaygroundCompletionRequest(
        messages=[{"role": "user", "content": "hi"}],
        tool_mocks=[{"tool_name": "x", "output": "y", "input_match_rule": "any"}],
        current_state="greeting",
        current_node_id="node_1",
    )
    resp = await run_playground_completion(
        app_session, _settings("proj"), agent_id=encode_agent_id(profile.id), version=None, request=req
    )
    dumped = resp.model_dump(exclude_none=True)
    assert list(dumped.keys()) == ["messages"]
    assert len(resp.messages) == 1


async def test_unknown_agent_raises_422(app_session) -> None:
    org = await _current_org(app_session)
    await set_tenant_context(app_session, org)
    with pytest.raises(CompatError) as ei:
        await run_playground_completion(
            app_session,
            _settings("proj"),
            agent_id=encode_agent_id(uuid.uuid4()),
            version=None,
            request=PlaygroundCompletionRequest(messages=[{"role": "user", "content": "hi"}]),
        )
    assert ei.value.status_code == 422


async def test_malformed_agent_id_raises_422(app_session) -> None:
    org = await _current_org(app_session)
    await set_tenant_context(app_session, org)
    with pytest.raises(CompatError) as ei:
        await run_playground_completion(
            app_session,
            _settings("proj"),
            agent_id="not-an-agent-id",
            version=None,
            request=PlaygroundCompletionRequest(messages=[{"role": "user", "content": "hi"}]),
        )
    assert ei.value.status_code == 422


async def test_no_gcp_project_raises_503(app_session) -> None:
    org = await _current_org(app_session)
    await set_tenant_context(app_session, org)
    profile = await _seed_published_profile(app_session)
    with pytest.raises(CompatError) as ei:
        await run_playground_completion(
            app_session,
            _settings(None),
            agent_id=encode_agent_id(profile.id),
            version=None,
            request=PlaygroundCompletionRequest(messages=[{"role": "user", "content": "hi"}]),
        )
    assert ei.value.status_code == 503
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_playground_service.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'usan_api.compat.playground_service'`.

- [ ] **Step 3: Write minimal implementation**

Create `apps/api/src/usan_api/compat/playground_service.py`:

```python
"""Service for the RetellAI-compat agent-playground-completion op (Phase 7 slice 1).

Stateless single text turn: resolve the org-scoped published AgentConfig, build a
system prompt, run one Vertex turn, return one agent message. Nothing persisted.
See docs/superpowers/specs/2026-07-01-retell-parity-phase7-playground-design.md.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.playground import (
    PlaygroundCompletionRequest,
    PlaygroundCompletionResponse,
    PlaygroundMessageOut,
)
from usan_api.prompt_substitution import build_vars, substitute
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.schemas.agent_config import AgentConfig
from usan_api.settings import Settings
from usan_api.vertex_test import run_vertex_turn


async def run_playground_completion(
    db: AsyncSession,
    settings: Settings,
    *,
    agent_id: str,
    version: str | None,
    request: PlaygroundCompletionRequest,
) -> PlaygroundCompletionResponse:
    # `version` is accepted and ignored — the currently-published config is served
    # (matches get-chat-agent). Malformed id → CompatError(422) inside the codec.
    profile_id = ids.decode_agent_id(agent_id)
    profile = await agent_profiles_repo.get_profile(db, profile_id)
    published = (
        await agent_profiles_repo.get_published_config(db, profile) if profile is not None else None
    )
    if published is None:
        # unknown / cross-org (RLS-filtered) / unpublished — no 404 for this op.
        raise CompatError(422, "agent is not available")
    cfg = AgentConfig.model_validate(published.config or {})

    values = build_vars({}, request.dynamic_variables or {}, timezone="", now=datetime.now(UTC))
    system_instruction = substitute(cfg.prompts.system_prompt, values)

    contents = [
        {"role": "model" if m.role == "agent" else "user", "parts": [{"text": m.content}]}
        for m in request.messages
        if m.content
    ]

    if not settings.gcp_project:
        raise CompatError(503, "playground completion unavailable")

    try:
        turn = await run_vertex_turn(
            model=cfg.llm.model,
            temperature=cfg.llm.temperature,
            system_instruction=system_instruction,
            tools=[],
            contents=contents,
            settings=settings,
        )
    except CompatError:
        raise
    except Exception as exc:  # noqa: BLE001 — PHI/secret-safe: type name only.
        logger.bind(err=type(exc).__name__).error("playground completion failed")
        raise CompatError(502, "playground completion failed") from None

    return PlaygroundCompletionResponse(
        messages=[
            PlaygroundMessageOut(
                message_id=str(uuid.uuid4()),
                content=turn.text,
                created_timestamp=int(time.time() * 1000),
            )
        ]
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_playground_service.py -q`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/compat/playground_service.py apps/api/tests/compat/test_playground_service.py
git commit -m "feat(api): run_playground_completion service (single-turn Vertex)"
```

---

### Task 3: Router, wiring, surface promotion, conformance, docs

**Files:**
- Create: `apps/api/src/usan_api/compat/routers/playground.py`
- Modify: `apps/api/src/usan_api/compat/app.py` (import + include_router)
- Modify: `apps/api/src/usan_api/compat/routers/unsupported.py` (delete the `_UNSUPPORTED` entry)
- Modify: `apps/api/tests/test_compat_fidelity.py` (delete the parametrize entry)
- Create: `apps/api/tests/compat/test_playground_endpoint.py`
- Create: `docs/deployment/playground-completion.md`

**Interfaces:**
- Consumes: `run_playground_completion` (Task 2); `get_compat_db`, `get_settings`, `Settings`; `assert_sdk_roundtrip`, `assert_conforms` (`tests/compat/conformance.py`); compat conftest fixtures `compat_client`, `compat_headers`, `gcp_project_set`, `gcp_project_unset`.
- Produces: served route `POST /agent-playground-completion/{agent_id}`.

- [ ] **Step 1: Write the failing endpoint + conformance test**

Create `apps/api/tests/compat/test_playground_endpoint.py`:

```python
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import create_engine, text

from usan_api.compat.ids import encode_agent_id
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG
from usan_api.settings import get_settings
from usan_api.vertex_test import VertexTurn

pytestmark = pytest.mark.usefixtures("gcp_project_set")

_CONFIG = DEFAULT_AGENT_CONFIG.model_dump()


def _seed_agent_via_superuser() -> str:
    """Insert a published agent_profiles row for the usan org via the superuser DSN
    (the compat_client's per-request session truncates at setup). Returns agent_<hex>."""
    import json

    sync_dsn = get_settings().async_database_url.replace("+asyncpg", "+psycopg")
    engine = create_engine(sync_dsn)
    profile_id = uuid.uuid4()
    try:
        with engine.begin() as conn:
            org_id = conn.execute(text("SELECT id FROM organizations LIMIT 1")).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO agent_profiles (id, organization_id, name, draft_config, "
                    "status, published_version, channel, created_at, updated_at) VALUES "
                    "(:id, :org, :name, :cfg, 'active', 1, 'voice', now(), now())"
                ),
                {"id": profile_id, "org": org_id, "name": "PG EP", "cfg": json.dumps(_CONFIG)},
            )
            conn.execute(
                text(
                    "INSERT INTO agent_profile_versions (id, organization_id, profile_id, "
                    "version, config, created_at) VALUES "
                    "(:id, :org, :pid, 1, :cfg, now())"
                ),
                {"id": uuid.uuid4(), "org": org_id, "pid": profile_id, "cfg": json.dumps(_CONFIG)},
            )
    finally:
        engine.dispose()
    return encode_agent_id(profile_id)


def test_endpoint_200_happy_path(compat_client, compat_headers, monkeypatch) -> None:
    async def fake_turn(**kwargs):
        return VertexTurn(text="Hi from playground")

    monkeypatch.setattr("usan_api.compat.playground_service.run_vertex_turn", fake_turn)
    agent_id = _seed_agent_via_superuser()
    r = compat_client.post(
        f"/agent-playground-completion/{agent_id}",
        headers=compat_headers,
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["messages"][0]["role"] == "agent"
    assert body["messages"][0]["content"] == "Hi from playground"
    assert "call_ended" not in body


@pytest.mark.frozen
def test_endpoint_response_conforms(compat_client, compat_headers, monkeypatch) -> None:
    from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip

    async def fake_turn(**kwargs):
        return VertexTurn(text="conformant text")

    monkeypatch.setattr("usan_api.compat.playground_service.run_vertex_turn", fake_turn)
    agent_id = _seed_agent_via_superuser()
    r = compat_client.post(
        f"/agent-playground-completion/{agent_id}",
        headers=compat_headers,
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert_sdk_roundtrip(payload, "retell.types:PlaygroundCompletionResponse")
    for msg in payload["messages"]:
        assert_conforms(msg, "MessageOrToolCall")


def test_endpoint_422_empty_messages(compat_client, compat_headers) -> None:
    agent_id = _seed_agent_via_superuser()
    r = compat_client.post(
        f"/agent-playground-completion/{agent_id}",
        headers=compat_headers,
        json={"messages": []},
    )
    assert r.status_code == 422, r.text


def test_endpoint_401_without_key(compat_client) -> None:
    r = compat_client.post(
        "/agent-playground-completion/agent_deadbeef",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401, r.text
```

> **Seed-helper note for the implementer:** the exact column set of `agent_profiles` / `agent_profile_versions` may differ (nullable defaults). If the raw INSERT fails, prefer the ORM seed used in `tests/compat/test_playground_service.py` executed against a superuser `AsyncSession` built from `get_settings().async_database_url` (see `tests/compat/conftest.py::published_default_agent` for the exact engine/`AsyncSession` pattern) rather than hand-writing SQL. The behavior asserted (200/422/401/frozen conformance) is what matters.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_playground_endpoint.py -q`
Expected: FAIL — the route returns 501 (still in `_UNSUPPORTED`) so `test_endpoint_200_happy_path` gets 501 not 200; import of the router module does not yet exist.

- [ ] **Step 3: Create the router**

Create `apps/api/src/usan_api/compat/routers/playground.py`:

```python
"""RetellAI-compat agent-playground-completion router (Phase 7 slice 1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import playground_service
from usan_api.compat.auth import get_compat_db
from usan_api.compat.schemas.playground import PlaygroundCompletionRequest
from usan_api.settings import Settings, get_settings

router = APIRouter(tags=["compat-playground"])


@router.post("/agent-playground-completion/{agent_id}")
async def agent_playground_completion(
    agent_id: str,
    body: PlaygroundCompletionRequest,
    request: Request,
    version: str | None = Query(default=None),
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    result = await playground_service.run_playground_completion(
        db, settings, agent_id=agent_id, version=version, request=body
    )
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op="agent-playground-completion").info(
        "compat playground op"
    )
    return result.model_dump(exclude_none=True)
```

- [ ] **Step 4: Wire the router into the compat sub-app**

In `apps/api/src/usan_api/compat/app.py`, next to the existing `from usan_api.compat.routers import chat_agents as compat_chat_agents` import, add:

```python
from usan_api.compat.routers import playground as compat_playground
```

and next to `app.include_router(compat_chat_agents.router)` add:

```python
app.include_router(compat_playground.router)
```

- [ ] **Step 5: Promote out of the 501 router**

In `apps/api/src/usan_api/compat/routers/unsupported.py`, delete this line from the `_UNSUPPORTED` tuple:

```python
        ("POST", "/agent-playground-completion/{agent_id}"),
```

In `apps/api/tests/test_compat_fidelity.py`, delete this entry from the `test_out_of_scope_returns_501_envelope` parametrize list:

```python
        ("post", "/agent-playground-completion/some-agent-id"),
```

- [ ] **Step 6: Run the endpoint + surface tests**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_playground_endpoint.py tests/compat/test_surface_coverage.py tests/test_compat_fidelity.py -q`
Expected: PASS. `test_surface_coverage.py` stays green automatically (op now served, not in `_UNSUPPORTED`, `KNOWN_GAPS` empty); the fidelity 501 test no longer parametrizes the promoted path.

- [ ] **Step 7: Write the deployment doc**

Create `docs/deployment/playground-completion.md`:

```markdown
# agent-playground-completion (Phase 7 slice 1)

`POST /agent-playground-completion/{agent_id}` — a stateless, single-turn Vertex
completion. Takes a `messages` history + optional `dynamic_variables`; returns one
agent message. RetellAI clients calling `retell.playground.completion(...)` reach it
with zero changes.

## Gates (ships inert)

- **Compat-key auth** — the whole compat surface 401s until a super-admin mints a key.
- **`GCP_PROJECT`** — 503 ("playground completion unavailable") until set; the Vertex
  turn runs via ADC (`vertexai=True`), never the Gemini Developer API (PHI containment).

No migration, no new env key. Not live until a `v*` tag deploy.

## Accepted-and-ignored (POSTURE)

| Field | Handling |
|-------|----------|
| `?version` | ignored — currently-published config served |
| `tool_mocks` | ignored — single `tools=[]` turn |
| `current_state` / `current_node_id` / `component_id` | ignored — no state/flow execution |
| response `current_state` / `current_node_id` / `dynamic_variables` / `call_ended` / `knowledge_base_retrieved_contents` | omitted (`exclude_none`) |

Unknown / cross-org / unpublished agent → **422** "agent is not available" (this op
declares no 404). Nothing is persisted or sent; only `type(exc).__name__` is logged on
failure.
```

- [ ] **Step 8: Full gate + commit**

Run:
```bash
cd apps/api && ruff check . && ruff format --check . && uv run mypy && uv run pytest -q
```
Expected: ruff clean, mypy clean, full suite green (playground tests included; chat/surface suites unaffected), single alembic head unchanged (no migration added).

```bash
git add apps/api/src/usan_api/compat/routers/playground.py \
        apps/api/src/usan_api/compat/app.py \
        apps/api/src/usan_api/compat/routers/unsupported.py \
        apps/api/tests/test_compat_fidelity.py \
        apps/api/tests/compat/test_playground_endpoint.py \
        docs/deployment/playground-completion.md
git commit -m "feat(api): serve agent-playground-completion (router, wiring, surface, docs)"
```

---

## Plan Self-Review

**Spec coverage:**
- §1 goal (LIVE single-turn) → Task 2 service + Task 3 endpoint. ✓
- §2 accepted-and-ignored (version/tool_mocks/state/node/component; omitted response fields) → Task 1 schema (`extra="allow"`, optional fields default None) + Task 2 (fields unread, `contents` skips content-less) + `test_advanced_fields_ignored` / `test_response_exclude_none_omits_unproduced_fields`. ✓
- §3 oracle surface (path, `?version`, request/response) → Task 1 + Task 3 route. ✓
- §4 architecture (router → service → Vertex; no `services/agent`) → Tasks 2–3. ✓
- §5 data flow (resolve→prompt→contents→503→vertex→response) → Task 2 body, in order. ✓
- §6 error handling (422 malformed, 422 not-available, 503, 502 wrap; no rollback/commit) → Task 2 + service tests. ✓
- §7 conformance (`assert_sdk_roundtrip` + `assert_conforms(MessageOrToolCall)`, `@pytest.mark.frozen`) → Task 3 `test_endpoint_response_conforms`; service/endpoint tests enumerated. ✓
- §8 deployment posture (inert, no migration/env key, doc) → Task 3 Step 7 doc. ✓
- §9 constitution → Global Constraints + PHI-safe except block. ✓

**Placeholder scan:** No TBD/TODO; every code step has complete code. The one advisory note (Task 3 seed-helper) gives a concrete ORM fallback with an exact reference (`published_default_agent`), not a hand-wave. ✓

**Type consistency:** `run_playground_completion(db, settings, *, agent_id, version, request) -> PlaygroundCompletionResponse` identical in Task 2 interface, impl, and all call sites (service tests + router). `PlaygroundMessageOut(message_id, role, content, created_timestamp)` identical in Task 1 def and Task 2 construction. Monkeypatch target `usan_api.compat.playground_service.run_vertex_turn` identical across all tests. ✓
