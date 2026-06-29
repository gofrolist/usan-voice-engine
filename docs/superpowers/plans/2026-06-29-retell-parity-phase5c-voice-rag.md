# Phase 5c — Voice-RAG retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring 5b's bound-`knowledge_base_ids` retrieval to the live voice pipeline — each user turn retrieves KB context (server-side, RLS-scoped) and injects it into that turn's LLM context before the spoken reply.

**Architecture:** The voice worker (`services/agent`, stateless, HTTP-only) calls a new internal endpoint `POST /v1/tools/retrieve_kb_context` from a per-turn hook. The server re-derives org (RLS, fail-closed) and `kb_ids` (`resolve_agent_config`) — the worker is trusted for neither, sending only `{call_id, query}` — and reuses 5b's `retrieve_context`. The worker injects the returned text into the turn via a `RagAgent(Agent)` subclass overriding `on_user_turn_completed`.

**Tech Stack:** FastAPI + SQLAlchemy + pgvector (apps/api, py314); LiveKit Agents 1.x + httpx (services/agent, py312); Vertex `text-embedding-005` (server-side only).

**Spec:** `docs/superpowers/specs/2026-06-29-retell-parity-phase5c-voice-rag-design.md`. **Branch:** `retell-parity-phase5c-voice-rag` (base main `33aa381`, spec `bd240ce`).

## Global Constraints

Every task's requirements implicitly include these (copied verbatim from the spec):

- **PHI/secret-safe logging only**, on BOTH services: never log chunk text, query text, source titles, or kb ids — counts + bucketed/rounded distances only (reuse 5b's `retrieve_context` logging). `call_id` may be bound (existing convention).
- **`organization_id` is server-set by RLS**, never by app code.
- **`apps/api` and `services/agent` never import each other** — the HTTP boundary is the only link.
- **`exclude_none`** discipline on serialized responses (the new response model has no optional fields, so this is a no-op there, but honor it).
- **CI mypy = `uv run mypy`** (config `files=["src"]`) — NEVER `mypy .`. Run for BOTH `apps/api` and `services/agent`.
- **ruff line-length 100**, target py314 (`apps/api`) / py312 (`services/agent`).
- This env's text display **strips parens from `except (A, B):`** — verify syntax via `python -m py_compile`, not by eye.
- **Forward-compat**: `apps/api` `AgentConfig` is frozen + re-validated on every read, and the `services/agent` `LLMConfig` ignores extras — any new field MUST be Optional with a default.
- **`apps/api` pytest runs `-n auto`** (parallel): cross-org/global-state tests must tolerate sibling rows; use the existing `two_orgs`/`app_session` fixtures + `set_tenant_context`. CI's `usan` superuser **BYPASSES RLS**, so cross-org tests MUST run on the non-superuser `app_session` fixture to be meaningful.
- **`services/agent` pytest** is `asyncio_mode="auto"`; mock LLM/session/httpx via `AsyncMock`/`MagicMock`/`monkeypatch` per existing tests.
- **NO new alembic migration** — `kb_ids` rides the existing config JSONB; single head stays `0047`.
- **Ships INERT** — both `kb_retrieval_voice_enabled` default `False`; **no `v*` tag** this phase. `KNOWN_GAPS` stays `frozenset()`.
- **Commit format** `type(scope): description`, scopes `api`/`agent`/`infra`/`ci`/`docs`. **Attribution disabled** (no `Co-Authored-By`, no footer).

### Deviation from the spec (documented for review)

The spec suggested a **dedicated** per-call retrieval rate ceiling. On inspection, the tool router applies `_enforce_tool_call_rate` as a **router-level** dependency and `tool_call_rate` is a **per-minute rate** (`"120/minute"`), not a per-call total. Human-paced turns produce ≤~12 calls/min even combined with tools — far under 120/min, and the window resets each minute — so the starvation concern does not apply. This plan therefore puts the endpoint on the existing `router` and **inherits the shared per-minute ceiling** (no new setting). The ceiling still bounds a runaway/hijacked agent's embed spend (≤120 embeds/min/call).

---

### Task 1: Parameterize `retrieve_context` with an explicit `enabled` flag

Makes the retrieval gate the **channel's** responsibility so voice and chat gate independently. Chat behavior is otherwise unchanged.

**Files:**
- Modify: `apps/api/src/usan_api/compat/kb_retrieval.py:50-53`
- Modify: `apps/api/src/usan_api/compat/chat_service.py:206-226`
- Test: `apps/api/tests/compat/test_kb_retrieval.py` (update existing call sites)

**Interfaces:**
- Produces: `retrieve_context(db, settings, *, kb_ids: list[str], query: str, enabled: bool) -> RetrievedContext` (new required kw-only `enabled`).
- Consumers updated this task: `chat_service.generate_agent_reply` (passes `enabled=settings.kb_retrieval_enabled`). Task 3's endpoint will pass `enabled=settings.kb_retrieval_voice_enabled`.

- [ ] **Step 1: Update the existing 5b retrieval tests for the new signature**

In `apps/api/tests/compat/test_kb_retrieval.py`, every call to `retrieve_context(...)` must add the `enabled=` kwarg, preserving each test's intent:
- Tests that exercise the happy path / a populated result → add `enabled=True`.
- The test that asserts the disabled gate returns empty → change it to pass `enabled=False` (instead of, or in addition to, monkeypatching `settings.kb_retrieval_enabled`). Keep one test that with `enabled=True` but `settings.gcp_project` unset still returns empty (the gcp_project gate is unchanged).

Apply this transformation to each `retrieve_context(` call in the file. Example — a call that read:

```python
result = await kb_retrieval.retrieve_context(db, settings, kb_ids=["knowledge_base_x"], query="q")
```

becomes:

```python
result = await kb_retrieval.retrieve_context(
    db, settings, kb_ids=["knowledge_base_x"], query="q", enabled=True
)
```

- [ ] **Step 2: Run the updated tests to verify they FAIL**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_kb_retrieval.py -q`
Expected: FAIL — `retrieve_context() got an unexpected keyword argument 'enabled'`.

- [ ] **Step 3: Change `retrieve_context` to take `enabled` and drop the internal flag read**

In `apps/api/src/usan_api/compat/kb_retrieval.py`, change the signature and the first gate line. Current (lines 50-53):

```python
async def retrieve_context(
    db: AsyncSession, settings: Settings, *, kb_ids: list[str], query: str
) -> RetrievedContext:
    if not settings.kb_retrieval_enabled or not settings.gcp_project or not kb_ids:
        return _EMPTY
```

becomes:

```python
async def retrieve_context(
    db: AsyncSession, settings: Settings, *, kb_ids: list[str], query: str, enabled: bool
) -> RetrievedContext:
    # `enabled` is the CHANNEL's gate: chat passes settings.kb_retrieval_enabled, voice
    # passes settings.kb_retrieval_voice_enabled. gcp_project stays the egress hard-gate.
    if not enabled or not settings.gcp_project or not kb_ids:
        return _EMPTY
```

Leave the rest of the function (the blank-query gate, decode loop, embed, search, assemble, PHI-safe log) unchanged.

- [ ] **Step 4: Update the chat caller to pass `enabled`**

In `apps/api/src/usan_api/compat/chat_service.py`, the retrieval block (lines 206-226) keeps its outer flag gate and savepoint; only the `retrieve_context(...)` call gains `enabled=`. Current call (line ~215):

```python
                retrieved = await retrieve_context(db, settings, kb_ids=kb_ids, query=query_text)
```

becomes:

```python
                retrieved = await retrieve_context(
                    db, settings, kb_ids=kb_ids, query=query_text,
                    enabled=settings.kb_retrieval_enabled,
                )
```

(The outer `if kb_ids and settings.kb_retrieval_enabled:` gate stays — it still short-circuits before the savepoint when disabled.)

- [ ] **Step 5: Verify syntax of the edited files**

Run: `cd apps/api && uv run python -m py_compile src/usan_api/compat/kb_retrieval.py src/usan_api/compat/chat_service.py`
Expected: no output (exit 0).

- [ ] **Step 6: Run the retrieval + chat-service tests to verify they PASS**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_kb_retrieval.py tests/compat/test_chat_service.py -q`
Expected: PASS (including the 5b DB-abort regression test `test_create_chat_completion_survives_db_abort_in_retrieval`).

- [ ] **Step 7: Commit**

```bash
git add apps/api/src/usan_api/compat/kb_retrieval.py apps/api/src/usan_api/compat/chat_service.py apps/api/tests/compat/test_kb_retrieval.py
git commit -m "refactor(api): parameterize retrieve_context with an explicit enabled flag"
```

---

### Task 2: Request/response schemas for the retrieval endpoint

**Files:**
- Modify: `apps/api/src/usan_api/schemas/tools.py` (append two models)
- Test: `apps/api/tests/test_schemas_tools.py` (create if absent, else add to the existing schema test module)

**Interfaces:**
- Produces: `RetrieveKbContextRequest(ToolCallRequest)` with `query: str`; `RetrieveKbContextResponse(BaseModel)` with `context: str`, `hit_count: int`.
- `ToolCallRequest` (existing, `schemas/tools.py:9-16`) supplies `call_id: uuid.UUID`.

- [ ] **Step 1: Write the failing test**

Create/extend `apps/api/tests/test_schemas_tools.py`:

```python
import uuid

import pytest
from pydantic import ValidationError

from usan_api.schemas.tools import RetrieveKbContextRequest, RetrieveKbContextResponse


def test_retrieve_kb_context_request_carries_call_id_and_query():
    cid = uuid.uuid4()
    req = RetrieveKbContextRequest(call_id=cid, query="how do I reset my pin")
    assert req.call_id == cid
    assert req.query == "how do I reset my pin"


def test_retrieve_kb_context_request_rejects_overlong_query():
    with pytest.raises(ValidationError):
        RetrieveKbContextRequest(call_id=uuid.uuid4(), query="x" * 4001)


def test_retrieve_kb_context_request_allows_empty_query():
    # Empty is allowed at the schema layer; retrieve_context's blank-query gate returns empty.
    req = RetrieveKbContextRequest(call_id=uuid.uuid4(), query="")
    assert req.query == ""


def test_retrieve_kb_context_response_shape():
    resp = RetrieveKbContextResponse(context="some context", hit_count=2)
    assert resp.context == "some context"
    assert resp.hit_count == 2
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd apps/api && uv run pytest -n0 tests/test_schemas_tools.py -q`
Expected: FAIL — `ImportError: cannot import name 'RetrieveKbContextRequest'`.

- [ ] **Step 3: Add the models**

Append to `apps/api/src/usan_api/schemas/tools.py` (after `CloseFamilyTaskResponse`):

```python
class RetrieveKbContextRequest(ToolCallRequest):
    # The worker sends the latest user utterance. The server re-derives kb_ids + org
    # itself (server-authoritative); the worker is trusted for neither. max_length caps
    # a hijacked-agent abuse path on the per-turn embed. Empty is allowed (retrieve_context
    # returns an empty result for a blank query).
    query: str = Field(max_length=4000)


class RetrieveKbContextResponse(BaseModel):
    # Invisible RAG: the assembled context block + a count. Never echoes kb ids or titles.
    context: str
    hit_count: int
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/api && uv run pytest -n0 tests/test_schemas_tools.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/schemas/tools.py apps/api/tests/test_schemas_tools.py
git commit -m "feat(api): add RetrieveKbContext request/response schemas"
```

---

### Task 3: The `kb_retrieval_voice_enabled` setting + the `POST /v1/tools/retrieve_kb_context` endpoint

**Files:**
- Modify: `apps/api/src/usan_api/settings.py:276` (insert after `kb_retrieval_max_context_chars`)
- Modify: `apps/api/src/usan_api/routers/tools.py` (import + endpoint)
- Test: `apps/api/tests/test_settings.py` (the new flag), and the existing tool-endpoint test module (the endpoint). Find it with `ls apps/api/tests | grep -i tool`; it is the module that already tests `/v1/tools/*` (mints a service token, creates a call). Read it + `apps/api/tests/kb_helpers.py` + `apps/api/tests/compat/test_agent_bridge.py` to learn the call/profile/KB seeding fixtures (`two_orgs`, `app_session`, `set_tenant_context`).

**Interfaces:**
- Consumes: `retrieve_context(..., enabled=...)` (Task 1); `RetrieveKbContextRequest`/`RetrieveKbContextResponse` (Task 2); existing `_authorize_call`, `profiles_repo.resolve_agent_config`, `contacts_repo.get_contact`, `DEFAULT_AGENT_CONFIG`, `require_service_token`, `get_settings`, `track_tool`.
- Produces: `POST /v1/tools/retrieve_kb_context` returning `{context, hit_count}`.

- [ ] **Step 1: Add the setting**

In `apps/api/src/usan_api/settings.py`, immediately after the `kb_retrieval_max_context_chars` field (line 276), add:

```python
    # Knowledge-base VOICE retrieval (Phase 5c). Separate from kb_retrieval_enabled so the
    # latency-sensitive live-voice path can be rolled out independently of chat. Ship-inert:
    # default OFF, so the voice retrieval endpoint embeds/searches nothing until a deploy
    # enables it AND gcp_project is set. Reuses kb_retrieval_top_k / max_distance /
    # max_context_chars / kb_embedding_* (one tuning set for both channels).
    kb_retrieval_voice_enabled: bool = Field(
        default=False, alias="KB_RETRIEVAL_VOICE_ENABLED"
    )
```

- [ ] **Step 2: Write the failing setting test**

Add to `apps/api/tests/test_settings.py` (mirror how the file builds `Settings()` — it uses a base-env helper + `monkeypatch`):

```python
def test_kb_retrieval_voice_enabled_defaults_false(monkeypatch):
    _base_env(monkeypatch)  # the file's existing helper that sets required env vars
    s = Settings()
    assert s.kb_retrieval_voice_enabled is False


def test_kb_retrieval_voice_enabled_reads_alias(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("KB_RETRIEVAL_VOICE_ENABLED", "true")
    s = Settings()
    assert s.kb_retrieval_voice_enabled is True
```

(If the file's helper has a different name, use that name — read the file first.)

- [ ] **Step 3: Run the setting test to verify it passes**

Run: `cd apps/api && uv run pytest -n0 tests/test_settings.py -k kb_retrieval_voice -q`
Expected: PASS.

- [ ] **Step 4: Write the failing endpoint tests**

In the tool-endpoint test module, add tests that use its existing helpers to create a call (default org) and mint a service token scoped to it, plus the KB seeding helpers from `tests/kb_helpers.py`. Name the helpers as they exist in the file; the asserted behavior is:

```python
# 1) bound kb + voice flag ON + gcp_project set -> context returned.
async def test_retrieve_kb_context_returns_context_when_enabled(...):
    # seed a KB + chunk for the default org; publish a profile that binds its kb id and
    # attach the profile to the call (the same way test_agent_bridge / existing tool tests do);
    # set settings.kb_retrieval_voice_enabled True and gcp_project; monkeypatch
    # kb_retrieval.embed_query to return a matching unit vector (no real Vertex call).
    resp = await client.post("/v1/tools/retrieve_kb_context",
        json={"call_id": str(call_id), "query": "hello"},
        headers={"Authorization": f"Bearer {service_token(call_id)}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["hit_count"] >= 1
    assert body["context"]

# 2) voice flag OFF -> empty (no embed).
async def test_retrieve_kb_context_empty_when_flag_off(...):
    # same setup but settings.kb_retrieval_voice_enabled False
    assert resp.json() == {"context": "", "hit_count": 0}

# 3) no kb_ids bound -> empty.
async def test_retrieve_kb_context_empty_when_no_kb_bound(...):
    # profile (or default config) with llm.knowledge_base_ids None
    assert resp.json() == {"context": "", "hit_count": 0}

# 4) blank query -> empty (no embed).
async def test_retrieve_kb_context_empty_on_blank_query(...):
    # flag ON, kb bound, query=""  (allowed by the schema)
    assert resp.json() == {"context": "", "hit_count": 0}

# 5) token call_id != body call_id -> 403.
async def test_retrieve_kb_context_rejects_token_not_for_call(...):
    other = uuid.uuid4()
    resp = await client.post("/v1/tools/retrieve_kb_context",
        json={"call_id": str(other), "query": "x"},
        headers={"Authorization": f"Bearer {service_token(call_id)}"})
    assert resp.status_code == 403

# 6) unknown call_id -> 404 (token scoped to it, but no such call). _authorize_call's
#    cross-org isolation is the same helper all tool endpoints use; deep cross-org chunk
#    isolation is already proven by 5b's test_search_chunks_cross_org_isolation.
async def test_retrieve_kb_context_unknown_call_404(...):
    cid = uuid.uuid4()
    resp = await client.post("/v1/tools/retrieve_kb_context",
        json={"call_id": str(cid), "query": "x"},
        headers={"Authorization": f"Bearer {service_token(cid)}"})
    assert resp.status_code == 404

# 7) retrieval raises (DB-class) -> endpoint returns empty 200, not 500.
async def test_retrieve_kb_context_degrades_on_retrieval_error(monkeypatch, ...):
    # flag ON, kb bound; monkeypatch the endpoint's retrieve_context to raise RuntimeError.
    import usan_api.routers.tools as tools_mod
    async def _boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(tools_mod, "retrieve_context", _boom)
    resp = await client.post("/v1/tools/retrieve_kb_context",
        json={"call_id": str(call_id), "query": "x"},
        headers={"Authorization": f"Bearer {service_token(call_id)}"})
    assert resp.status_code == 200
    assert resp.json() == {"context": "", "hit_count": 0}
```

Use the test module's real fixtures/helpers for `client`, `service_token`, call/profile/KB creation. For tests 1-4 and 7, set `settings.kb_retrieval_voice_enabled` and `settings.gcp_project` via the file's settings-override mechanism (a `get_settings` dependency override, or monkeypatching the cached settings — match the existing tests).

- [ ] **Step 5: Run the endpoint tests to verify they fail**

Run: `cd apps/api && uv run pytest -n0 <tool_endpoint_test_module> -k retrieve_kb_context -q`
Expected: FAIL — 404 (route not registered).

- [ ] **Step 6: Implement the endpoint**

In `apps/api/src/usan_api/routers/tools.py`, add the imports (extend the existing schema import block and add the kb_retrieval import):

```python
from usan_api.compat.kb_retrieval import RetrievedContext, retrieve_context
```

and add `RetrieveKbContextRequest, RetrieveKbContextResponse` to the existing `from usan_api.schemas.tools import (...)` block.

Add the endpoint (place it near the other call-scoped tool endpoints, e.g. after `send_sms`):

```python
@router.post("/retrieve_kb_context", response_model=RetrieveKbContextResponse)
@track_tool("retrieve_kb_context")
async def retrieve_kb_context(
    body: RetrieveKbContextRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
    settings: Settings = Depends(get_settings),
) -> RetrieveKbContextResponse:
    """Voice-RAG (Phase 5c): retrieve KB context for the worker's current turn.

    Server-authoritative: org is bound by RLS (via the call, fail-closed) and kb_ids are
    re-derived from the resolved profile — the worker sends only {call_id, query}. Read-only
    and best-effort: any retrieval failure degrades to empty context (never 500s the call).
    """
    call = await _authorize_call(body.call_id, claims, db)
    contact = (
        await contacts_repo.get_contact(db, call.contact_id)
        if call.contact_id is not None
        else None
    )
    resolved = await profiles_repo.resolve_agent_config(
        db,
        profile_override=call.profile_override,
        contact_profile_id=contact.agent_profile_id if contact is not None else None,
        direction=call.direction.value,
    )
    cfg = resolved.config if resolved is not None else DEFAULT_AGENT_CONFIG
    kb_ids = cfg.llm.knowledge_base_ids or []
    try:
        retrieved = await retrieve_context(
            db,
            settings,
            kb_ids=kb_ids,
            query=body.query,
            enabled=settings.kb_retrieval_voice_enabled,
        )
    except Exception as exc:  # best-effort: retrieval NEVER breaks a call
        logger.bind(err=type(exc).__name__, kb_count=len(kb_ids)).warning(
            "voice kb retrieval failed; returning empty context"
        )
        retrieved = RetrievedContext("", 0)
    return RetrieveKbContextResponse(context=retrieved.text, hit_count=retrieved.hit_count)
```

- [ ] **Step 7: Verify syntax**

Run: `cd apps/api && uv run python -m py_compile src/usan_api/routers/tools.py src/usan_api/settings.py`
Expected: no output (exit 0).

- [ ] **Step 8: Run the endpoint tests to verify they pass**

Run: `cd apps/api && uv run pytest -n0 <tool_endpoint_test_module> -k retrieve_kb_context tests/test_settings.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add apps/api/src/usan_api/settings.py apps/api/src/usan_api/routers/tools.py apps/api/tests/
git commit -m "feat(api): add POST /v1/tools/retrieve_kb_context for voice-RAG"
```

---

### Task 4: Agent-side config plumbing — `LLMConfig.knowledge_base_ids` + voice settings

**Files:**
- Modify: `services/agent/src/usan_agent/agent_config.py:37-39` (`LLMConfig`)
- Modify: `services/agent/src/usan_agent/settings.py` (two fields)
- Test: `services/agent/tests/test_agent_config.py` (create if absent), `services/agent/tests/test_settings.py` (the worker's settings test — find it; create if absent)

**Interfaces:**
- Produces: `LLMConfig.knowledge_base_ids: list[str] | None` (default None); `Settings.kb_retrieval_voice_enabled: bool` (default False); `Settings.kb_retrieval_timeout_s: float` (default 3.0). Consumed by Tasks 5 (timeout) and 6 (flag + kb_ids gate).

- [ ] **Step 1: Write the failing config + settings tests**

`services/agent/tests/test_agent_config.py`:

```python
from usan_agent.agent_config import AgentConfig, DEFAULT_AGENT_CONFIG, LLMConfig


def test_llm_config_knowledge_base_ids_defaults_none():
    assert LLMConfig().knowledge_base_ids is None


def test_llm_config_parses_knowledge_base_ids():
    cfg = LLMConfig.model_validate({"model": "m", "knowledge_base_ids": ["knowledge_base_a"]})
    assert cfg.knowledge_base_ids == ["knowledge_base_a"]


def test_old_config_without_knowledge_base_ids_still_validates():
    # Forward-compat: a published config produced before the field existed must parse.
    raw = DEFAULT_AGENT_CONFIG.model_dump()
    raw["llm"].pop("knowledge_base_ids", None)
    cfg = AgentConfig.model_validate(raw)
    assert cfg.llm.knowledge_base_ids is None
```

`services/agent/tests/test_settings.py` (mirror the worker settings test pattern; the worker `Settings` requires the env vars in `settings.py:17-30` — set them via `monkeypatch.setenv`, or reuse the file's helper if present):

```python
def test_kb_retrieval_voice_settings_defaults(monkeypatch):
    _set_required_env(monkeypatch)  # sets LIVEKIT_*, CARTESIA_API_KEY, GCP_PROJECT,
                                     # DEFAULT_CARTESIA_VOICE_ID, API_BASE_URL, JWT_SIGNING_KEY
    s = Settings()
    assert s.kb_retrieval_voice_enabled is False
    assert s.kb_retrieval_timeout_s == 3.0
```

(If no settings test file/helper exists, create the file and a small `_set_required_env` that sets every required alias from `settings.py` with valid dummy values — `JWT_SIGNING_KEY` and `LIVEKIT_API_SECRET` need length ≥ 32, `API_BASE_URL=http://api:8000`, `LIVEKIT_URL=ws://lk`.)

- [ ] **Step 2: Run to verify failure**

Run: `cd services/agent && uv run pytest tests/test_agent_config.py tests/test_settings.py -q`
Expected: FAIL (AttributeError / field missing).

- [ ] **Step 3: Add the `LLMConfig` field**

In `services/agent/src/usan_agent/agent_config.py`, change `LLMConfig` (lines 37-39) to:

```python
class LLMConfig(BaseModel):
    model: str = "gemini-3.1-flash-lite"
    temperature: float | None = None
    # Phase 5c: KB ids bound to this agent (mirrors the API copy). Used by the worker ONLY
    # as a local gate (skip the per-turn retrieval HTTP call when empty); the ids themselves
    # are never sent — the server re-derives them. Optional+default per the forward-compat rule.
    knowledge_base_ids: list[str] | None = None
```

- [ ] **Step 4: Add the worker settings fields**

In `services/agent/src/usan_agent/settings.py`, add inside `Settings` (after `gcs_bucket`, before `log_level`):

```python
    # Voice-RAG (Phase 5c). kb_retrieval_voice_enabled gates whether the worker makes the
    # per-turn retrieve_kb_context call at all (a true no-op when off — no wasted round-trip);
    # the API has its own KB_RETRIEVAL_VOICE_ENABLED that is the real embed/search gate.
    # kb_retrieval_timeout_s bounds the per-turn retrieval HTTP call so a slow lookup can't
    # stall turn-taking; on timeout the worker speaks without injected context.
    kb_retrieval_voice_enabled: bool = Field(
        default=False, alias="KB_RETRIEVAL_VOICE_ENABLED"
    )
    kb_retrieval_timeout_s: float = Field(default=3.0, alias="KB_RETRIEVAL_TIMEOUT_S")
```

- [ ] **Step 5: Verify syntax**

Run: `cd services/agent && uv run python -m py_compile src/usan_agent/agent_config.py src/usan_agent/settings.py`
Expected: no output (exit 0).

- [ ] **Step 6: Run to verify pass**

Run: `cd services/agent && uv run pytest tests/test_agent_config.py tests/test_settings.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add services/agent/src/usan_agent/agent_config.py services/agent/src/usan_agent/settings.py services/agent/tests/test_agent_config.py services/agent/tests/test_settings.py
git commit -m "feat(agent): add knowledge_base_ids + voice-RAG settings (inert)"
```

---

### Task 5: Worker API client — `retrieve_kb_context`

**Files:**
- Modify: `services/agent/src/usan_agent/api_client.py` (new function, near `flush_transcript`/`post_metrics`)
- Test: `services/agent/tests/test_api_client_tools.py`

**Interfaces:**
- Consumes: `_mint_token`, `_validate_call_id`, `Settings.api_base_url`, `Settings.kb_retrieval_timeout_s` (Task 4).
- Produces: `async def retrieve_kb_context(call_id: str, settings: Settings, query: str) -> str` — returns the context block, or `""` on any error/timeout/non-200. Best-effort, never raises. PHI-safe (logs counts only). Consumed by Task 6.

- [ ] **Step 1: Write the failing tests**

Add to `services/agent/tests/test_api_client_tools.py` (mirror the `_FakeClient` / `fake_http` pattern at the top of the file):

```python
async def test_retrieve_kb_context_posts_scoped_request_and_returns_context(fake_http):
    _FakeClient.json_data = {"context": "kb says hello", "hit_count": 2}
    out = await api_client.retrieve_kb_context("call-1", _settings(), "how are meds taken")
    cap = fake_http.captured
    assert cap["url"] == "http://api:8000/v1/tools/retrieve_kb_context"
    assert cap["json"] == {"call_id": "call-1", "query": "how are meds taken"}
    assert cap["headers"]["Authorization"].startswith("Bearer ")
    assert out == "kb says hello"


async def test_retrieve_kb_context_returns_empty_on_http_error(fake_http):
    _FakeClient.raise_status = True  # the fake's flag that makes raise_for_status() throw
    out = await api_client.retrieve_kb_context("call-1", _settings(), "q")
    assert out == ""


async def test_retrieve_kb_context_returns_empty_on_missing_context_field(fake_http):
    _FakeClient.json_data = {"hit_count": 0}
    out = await api_client.retrieve_kb_context("call-1", _settings(), "q")
    assert out == ""
```

(Match the file's actual fake-client error-injection mechanism; if it raises differently, adapt the second test to trigger that path. `_settings()` is the file's existing settings factory — ensure it provides `kb_retrieval_timeout_s` now that the field exists; if `_settings()` constructs a real `Settings`, the new default 3.0 applies automatically.)

- [ ] **Step 2: Run to verify failure**

Run: `cd services/agent && uv run pytest tests/test_api_client_tools.py -k retrieve_kb_context -q`
Expected: FAIL — `AttributeError: module 'usan_agent.api_client' has no attribute 'retrieve_kb_context'`.

- [ ] **Step 3: Implement the function**

Add to `services/agent/src/usan_agent/api_client.py` (after `post_metrics`, before `start_inbound_call`):

```python
async def retrieve_kb_context(call_id: str, settings: Settings, query: str) -> str:
    """Best-effort voice-RAG context for the current turn. Returns "" on any failure.

    The server re-derives org + kb_ids; we send only {call_id, query}. Tight timeout so a
    slow lookup never stalls turn-taking. PHI-safe: logs only the hit count — never the
    query or the returned context. Never raises (the turn proceeds with no context on error).
    """
    try:
        call_id = _validate_call_id(call_id)
        url = f"{settings.api_base_url}/v1/tools/retrieve_kb_context"
        headers = {"Authorization": f"Bearer {_mint_token(call_id, settings)}"}
        async with httpx.AsyncClient(timeout=settings.kb_retrieval_timeout_s) as client:
            response = await client.post(
                url, json={"call_id": call_id, "query": query}, headers=headers
            )
            response.raise_for_status()
            body = response.json()
        context = body.get("context", "")
        logger.bind(call_id=call_id, hits=body.get("hit_count", 0)).debug(
            "kb retrieval hits={hits}"
        )
        return context if isinstance(context, str) else ""
    except Exception:
        logger.bind(call_id=call_id).warning("kb retrieval call failed; continuing without context")
        return ""
```

- [ ] **Step 4: Verify syntax**

Run: `cd services/agent && uv run python -m py_compile src/usan_agent/api_client.py`
Expected: no output (exit 0).

- [ ] **Step 5: Run to verify pass**

Run: `cd services/agent && uv run pytest tests/test_api_client_tools.py -k retrieve_kb_context -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add services/agent/src/usan_agent/api_client.py services/agent/tests/test_api_client_tools.py
git commit -m "feat(agent): add best-effort retrieve_kb_context API client"
```

---

### Task 6: `RagAgent` subclass + factory wiring (the injection point)

**Files:**
- Create: `services/agent/src/usan_agent/rag_agent.py`
- Modify: `services/agent/src/usan_agent/pipeline.py:109-115` (`build_agent`)
- Modify: `services/agent/src/usan_agent/check_in.py:974-1031` (`build_check_in_agent`, `build_inbound_agent`)
- Modify: `services/agent/src/usan_agent/worker.py` (pass `call_id`+`settings` at the two construction sites: outbound ~506, inbound ~142)
- Test: `services/agent/tests/test_rag_agent.py` (create)

**Interfaces:**
- Consumes: `Agent` (`livekit.agents.voice`), `llm.ChatContext`/`llm.ChatMessage`, `api_client.retrieve_kb_context` (Task 5), `Settings` (Task 4).
- Produces: `RagAgent(Agent)` constructed by the three factories. The hook gates on `enabled and call_id and kb_ids`, sends only `{call_id, query}`, injects via `turn_ctx.add_message(role="system", content=...)`, and is fully exception-guarded.

- [ ] **Step 1: Write the failing tests**

Create `services/agent/tests/test_rag_agent.py`:

```python
from unittest.mock import AsyncMock

import pytest
from livekit.agents import llm

from usan_agent import api_client
from usan_agent.rag_agent import RagAgent


def _settings():
    # Reuse the project's settings factory if one exists in conftest/tests; otherwise build a
    # minimal real Settings. kb_retrieval_voice_enabled / timeout come from defaults or here.
    from usan_agent.settings import Settings
    return Settings(
        LIVEKIT_API_KEY="k", LIVEKIT_API_SECRET="s" * 32, LIVEKIT_URL="ws://lk",
        CARTESIA_API_KEY="c", GCP_PROJECT="p", DEFAULT_CARTESIA_VOICE_ID="v",
        API_BASE_URL="http://api:8000", JWT_SIGNING_KEY="j" * 32,
    )


def _agent(*, enabled, call_id="call-1", kb_ids=("knowledge_base_a",)):
    return RagAgent(
        call_id=call_id,
        kb_ids=list(kb_ids) if kb_ids else [],
        settings=_settings(),
        enabled=enabled,
        instructions="be kind",
    )


def _user_msg(text):
    return llm.ChatMessage(role="user", content=[text])


async def test_injects_context_on_hit(monkeypatch):
    spy = AsyncMock(return_value="KB FACT")
    monkeypatch.setattr(api_client, "retrieve_kb_context", spy)
    agent = _agent(enabled=True)
    ctx = llm.ChatContext.empty()
    await agent.on_user_turn_completed(ctx, _user_msg("how do meds work"))
    spy.assert_awaited_once()
    # only call_id + query are passed to the client (positional: call_id, settings, query)
    assert spy.await_args.args[0] == "call-1"
    assert spy.await_args.args[2] == "how do meds work"
    # at least one system message now carries the injected context
    assert any(
        getattr(m, "role", None) == "system" and "KB FACT" in (m.text_content or "")
        for m in ctx.items
        if isinstance(m, llm.ChatMessage)
    )


async def test_no_call_when_disabled(monkeypatch):
    spy = AsyncMock(return_value="KB FACT")
    monkeypatch.setattr(api_client, "retrieve_kb_context", spy)
    agent = _agent(enabled=False)
    ctx = llm.ChatContext.empty()
    await agent.on_user_turn_completed(ctx, _user_msg("q"))
    spy.assert_not_awaited()


async def test_no_call_when_no_kb_bound(monkeypatch):
    spy = AsyncMock(return_value="KB FACT")
    monkeypatch.setattr(api_client, "retrieve_kb_context", spy)
    agent = _agent(enabled=True, kb_ids=())
    await agent.on_user_turn_completed(llm.ChatContext.empty(), _user_msg("q"))
    spy.assert_not_awaited()


async def test_no_call_when_no_call_id(monkeypatch):
    spy = AsyncMock(return_value="KB FACT")
    monkeypatch.setattr(api_client, "retrieve_kb_context", spy)
    agent = _agent(enabled=True, call_id=None)
    await agent.on_user_turn_completed(llm.ChatContext.empty(), _user_msg("q"))
    spy.assert_not_awaited()


async def test_no_injection_on_empty_context(monkeypatch):
    monkeypatch.setattr(api_client, "retrieve_kb_context", AsyncMock(return_value=""))
    agent = _agent(enabled=True)
    ctx = llm.ChatContext.empty()
    await agent.on_user_turn_completed(ctx, _user_msg("q"))
    assert all(getattr(m, "role", None) != "system" for m in ctx.items if isinstance(m, llm.ChatMessage))


async def test_retrieval_error_does_not_raise(monkeypatch):
    monkeypatch.setattr(api_client, "retrieve_kb_context", AsyncMock(side_effect=RuntimeError("x")))
    agent = _agent(enabled=True)
    # must not raise (an exception here would abort the turn)
    await agent.on_user_turn_completed(llm.ChatContext.empty(), _user_msg("q"))


async def test_no_call_on_blank_utterance(monkeypatch):
    spy = AsyncMock(return_value="KB FACT")
    monkeypatch.setattr(api_client, "retrieve_kb_context", spy)
    agent = _agent(enabled=True)
    await agent.on_user_turn_completed(llm.ChatContext.empty(), _user_msg(""))
    spy.assert_not_awaited()
```

(The exact `ChatContext` items accessor is `ctx.items`; if the installed version differs, adapt the assertion to inspect the appended message. `llm.ChatContext.empty()` and `llm.ChatMessage(role=..., content=[text])` are the verified constructors. `text_content` joins string content parts.)

- [ ] **Step 2: Run to verify failure**

Run: `cd services/agent && uv run pytest tests/test_rag_agent.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_agent.rag_agent'`.

- [ ] **Step 3: Implement `RagAgent`**

Create `services/agent/src/usan_agent/rag_agent.py`:

```python
"""Voice-RAG agent (Phase 5c).

A thin Agent subclass that, on each completed user turn, fetches knowledge-base context
from the API (server re-derives org + kb_ids — we send only call_id + query) and injects it
into the turn's chat context before the LLM responds. Ephemeral: the context is added to the
turn context for this generation only, not persisted into running history.

Everything is gated and exception-guarded: an exception raised in on_user_turn_completed
would ABORT the turn, so a retrieval failure must never escape this method.
"""

from __future__ import annotations

from typing import Any

from livekit.agents import llm
from livekit.agents.voice import Agent
from loguru import logger

from usan_agent import api_client
from usan_agent.settings import Settings

_CONTEXT_PREFIX = "Knowledge base context:\n"
_CONTEXT_SUFFIX = "\n\nUse the above context to answer when relevant."


class RagAgent(Agent):
    def __init__(
        self,
        *,
        call_id: str | None = None,
        kb_ids: list[str] | None = None,
        settings: Settings | None = None,
        enabled: bool = False,
        **agent_kwargs: Any,
    ) -> None:
        super().__init__(**agent_kwargs)
        # kb_ids is a LOCAL GATE only (skip the round-trip when nothing is bound); never sent.
        self._kb_call_id = call_id
        self._kb_ids = kb_ids or []
        self._kb_settings = settings
        self._kb_enabled = enabled

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        try:
            if not (
                self._kb_enabled
                and self._kb_call_id
                and self._kb_ids
                and self._kb_settings is not None
            ):
                return
            query = new_message.text_content
            if not query or not query.strip():
                return
            context = await api_client.retrieve_kb_context(
                self._kb_call_id, self._kb_settings, query
            )
            if context:
                turn_ctx.add_message(
                    role="system", content=_CONTEXT_PREFIX + context + _CONTEXT_SUFFIX
                )
        except Exception as exc:  # an exception here would abort the turn — swallow it
            logger.bind(err=type(exc).__name__).warning(
                "voice kb retrieval hook failed; continuing without context"
            )
```

- [ ] **Step 4: Wire `build_agent` (pipeline.py) to build `RagAgent`**

In `services/agent/src/usan_agent/pipeline.py`, add the import and update `build_agent`:

```python
from usan_agent.rag_agent import RagAgent
```

```python
def build_agent(
    cfg: AgentConfig | None = None,
    *,
    call_id: str | None = None,
    settings: Settings | None = None,
) -> Agent:
    """Construct the greet-only Agent with the configured system prompt (no tools)."""
    cfg = cfg or DEFAULT_AGENT_CONFIG
    return RagAgent(
        instructions=cfg.prompts.system_prompt,
        chat_ctx=ChatContext(),
        call_id=call_id,
        kb_ids=cfg.llm.knowledge_base_ids,
        settings=settings,
        enabled=bool(settings and settings.kb_retrieval_voice_enabled),
    )
```

(The return type stays `Agent` — `RagAgent` is an `Agent`. `Settings` is already imported in pipeline.py.)

- [ ] **Step 5: Wire the check-in factories (check_in.py) to build `RagAgent`**

In `services/agent/src/usan_agent/check_in.py`, add the import and thread `call_id`/`settings` through both factories:

```python
from usan_agent.rag_agent import RagAgent
```

`build_check_in_agent` — add params and swap the constructor:

```python
def build_check_in_agent(
    cfg: AgentConfig | None = None,
    *,
    resolved_vars: dict[str, str] | None = None,
    custom_vars: dict[str, Any] | None = None,
    timezone: str = "",
    now: datetime | None = None,
    call_id: str | None = None,
    settings: Settings | None = None,
) -> Agent:
    cfg = cfg or DEFAULT_AGENT_CONFIG
    values = build_vars(
        resolved_vars or {},
        custom_vars or {},
        timezone=timezone,
        now=now or datetime.now(UTC),
    )
    return RagAgent(
        instructions=substitute(cfg.prompts.checkin_flow_instructions, values)
        + _sms_template_instructions(cfg.tools),
        tools=_select_tools(cfg.tools),
        call_id=call_id,
        kb_ids=cfg.llm.knowledge_base_ids,
        settings=settings,
        enabled=bool(settings and settings.kb_retrieval_voice_enabled),
    )
```

Apply the identical change to `build_inbound_agent` (same new params; same `RagAgent(...)` call but with `instructions=substitute(cfg.prompts.inbound_personalization_template, values) + _sms_template_instructions(cfg.tools)`).

- [ ] **Step 6: Pass `call_id`+`settings` at the worker construction sites**

In `services/agent/src/usan_agent/worker.py`, the outbound site (~line 506) currently:

```python
        agent = build_check_in_agent(
            cfg,
            resolved_vars=meta.resolved_vars,
            custom_vars=meta.dynamic_vars,
            timezone=meta.timezone,
        )
```

becomes (add the two kwargs):

```python
        agent = build_check_in_agent(
            cfg,
            resolved_vars=meta.resolved_vars,
            custom_vars=meta.dynamic_vars,
            timezone=meta.timezone,
            call_id=call_id,
            settings=settings,
        )
```

The inbound site (~line 142) `build_inbound_agent(cfg, resolved_vars=..., custom_vars=dynamic_vars, timezone=...)` gains `call_id=call_id, settings=settings` (here `call_id = str(info["call_id"])` and `settings` is the function arg). Leave any greet-only `build_agent(...)` fallback calls as-is or pass `settings=settings` (call_id may be absent → hook no-ops); do not invent a call_id.

- [ ] **Step 7: Verify syntax**

Run: `cd services/agent && uv run python -m py_compile src/usan_agent/rag_agent.py src/usan_agent/pipeline.py src/usan_agent/check_in.py src/usan_agent/worker.py`
Expected: no output (exit 0).

- [ ] **Step 8: Run the RagAgent + existing pipeline/check_in tests to verify pass**

Run: `cd services/agent && uv run pytest tests/test_rag_agent.py tests/test_pipeline.py tests/test_check_in.py -q`
Expected: PASS. (The existing factory tests must still pass — `RagAgent` is an `Agent`, instructions/tools unchanged. If a test asserts `type(agent) is Agent`, update it to `isinstance(agent, Agent)`.)

- [ ] **Step 9: Commit**

```bash
git add services/agent/src/usan_agent/rag_agent.py services/agent/src/usan_agent/pipeline.py services/agent/src/usan_agent/check_in.py services/agent/src/usan_agent/worker.py services/agent/tests/test_rag_agent.py
git commit -m "feat(agent): inject voice-RAG context per turn via RagAgent (inert)"
```

---

### Task 7: Operator deployment note

**Files:**
- Create: `docs/deployment/voice-rag-retrieval.md`

**Interfaces:** none (documentation).

- [ ] **Step 1: Write the operator note**

Create `docs/deployment/voice-rag-retrieval.md` covering, in prose:

- **What it does:** when a voice agent's bound config has `knowledge_base_ids`, each user turn retrieves KB context (server-side, RLS-scoped) and injects it into that turn — invisible to the caller; answers only.
- **Ships inert + activation order:** both `KB_RETRIEVAL_VOICE_ENABLED` (apps/api = the real embed/search gate; services/agent = the per-turn-call gate) default `False`, and `GCP_PROJECT` must be set. To enable: set `KB_RETRIEVAL_VOICE_ENABLED=true` on BOTH services + ensure `GCP_PROJECT` is set, then deploy. The **compose-env-passthrough two-place rule**: the key must be in each service's compose `environment:` map AND the VM `.env`, or it silently no-ops.
- **Shared tunables:** voice reuses chat's `KB_RETRIEVAL_TOP_K` / `KB_RETRIEVAL_MAX_DISTANCE` / `KB_RETRIEVAL_MAX_CONTEXT_CHARS` and `KB_EMBEDDING_MODEL` / `KB_EMBEDDING_LOCATION`. `KB_RETRIEVAL_MAX_DISTANCE` (0.7 default) is a cosine-distance ceiling that MUST be tuned against real KB content. The worker's `KB_RETRIEVAL_TIMEOUT_S` (3.0 default) caps per-turn retrieval latency; on timeout the agent speaks without context.
- **VERIFY at deploy:** query-embed reachability from apps/api (Vertex `text-embedding-005` returns a 768-dim vector); a live call with a KB-bound profile produces context-aware answers; check counts-only logs (no PHI).
- **PHI/security:** RLS is the hard isolation guarantee (org is server-set; the worker sends only `{call_id, query}`); logs are counts/bucketed-distances only; retrieval is invisible (no new response surface). Best-effort everywhere — a retrieval failure never breaks a call.
- **Known limitations:** per-turn embed adds latency before the agent speaks (bounded by the timeout); voice reuses the chat context-size cap — a smaller voice-specific cap is a possible follow-up; the call plane is single-org today, so the endpoint inherits the tool plane's single-org RLS posture.

- [ ] **Step 2: Commit**

```bash
git add docs/deployment/voice-rag-retrieval.md
git commit -m "docs: operator note for voice-RAG retrieval (Phase 5c)"
```

---

## Final verification (after all tasks)

- [ ] `cd apps/api && uv run pytest` (full suite, parallel) — all pass.
- [ ] `cd apps/api && ruff check . && ruff format --check . && uv run mypy`
- [ ] `cd services/agent && uv run pytest -v` — all pass.
- [ ] `cd services/agent && ruff check . && ruff format --check . && uv run mypy`
- [ ] `cd apps/api && uv run alembic heads` shows a single head `0047` (no new migration).
- [ ] Confirm both `kb_retrieval_voice_enabled` defaults are `False` (ships inert); `KNOWN_GAPS` unchanged (`frozenset()`).

## Self-Review notes (author)

- **Spec coverage:** §6 endpoint → Task 3; §5b `enabled` refactor → Task 1; §7 worker (`LLMConfig`, `RagAgent`, factories, `api_client`) → Tasks 4-6; §8 settings → Tasks 3 (api) + 4 (agent); §9 error/latency/PHI → Tasks 5 (`""`-on-error), 6 (exception-guarded hook), 3 (degrade-to-empty); §10 testing → each task's tests; §12 docs → Task 7. The spec's "dedicated rate ceiling" is intentionally replaced by the shared per-minute ceiling (documented above) — flag at plan review.
- **Type consistency:** `retrieve_context(..., enabled: bool)` consistent in Tasks 1 & 3; `RetrieveKbContextRequest/Response` consistent Tasks 2-3; `RagAgent(*, call_id, kb_ids, settings, enabled, **agent_kwargs)` consistent Task 6 across all three factories + worker call sites; `retrieve_kb_context(call_id, settings, query) -> str` consistent Tasks 5-6.
- **Integration-test fixtures:** Task 3's endpoint tests and Task 6's `ChatContext` accessors reference existing fixtures/library APIs by name (the implementer reads the tool-test module, `tests/kb_helpers.py`, and the installed `livekit.agents.llm`) — standard for a plan touching an established harness; the exact assertions and setup steps are specified.
