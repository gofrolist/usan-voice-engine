# Phase 5c — Voice-RAG retrieval (design)

**Program:** RetellAI full-parity. **Phase:** 5c (the phase after 5b text-RAG for chat, merged as `33aa381` / PR #147).
**Status:** Approved design. Next step: implementation plan via `writing-plans`.
**Date:** 2026-06-29.

---

## 1. Goal

Bring 5b's bound-`knowledge_base_ids` retrieval to the **live voice pipeline** (`services/agent`,
LiveKit Agents 1.x worker, Python 3.12). When a voice agent's resolved config binds one or more
knowledge bases, each user turn retrieves the most relevant ingested chunks and injects them into
that turn's LLM context **before** the spoken reply is generated.

## 2. Parity framing

RetellAI's RAG is **internal** — there is no client-facing "query knowledge base" operation in its
API. Parity is therefore **behavioral, not surface**: a bound KB must influence the agent's spoken
answers. Phase 5c adds **zero new client-facing API operations**. The one new endpoint
(`POST /v1/tools/retrieve_kb_context`) is an **internal worker→api operation** on our native API,
not part of the RetellAI-compatible sub-app. `KNOWN_GAPS` stays `frozenset()`.

## 3. Locked decisions

1. **Retrieval runs in a new internal `apps/api` endpoint** that the voice worker calls over HTTP.
   The worker is stateless and HTTP-only (no DB, no embedding capability today); putting retrieval
   server-side reuses 5b's stack, keeps **RLS** as the hard tenant-isolation guarantee, and adds no
   new worker dependencies. (Rejected: a DB+embedding connection in the worker — it would break the
   stateless architecture, add PHI DB access to the voice worker, duplicate KB logic across the
   `apps/api`⊥`services/agent` boundary, and replace RLS with hand-rolled `organization_id`
   filtering. Rejected: an LLM function-tool — it costs a second LLM round-trip per use, depends on
   the model deciding to call it, and is weaker parity than automatic inline injection.)
2. **Separate `kb_retrieval_voice_enabled` flag**, independent of chat's `kb_retrieval_enabled`, so
   voice (latency-sensitive, live on real calls) can be validated and rolled out on its own.
3. **Ephemeral per-turn injection**: retrieved context is injected only for the current LLM
   generation (via the turn's context), never persisted into the running chat history. Each turn
   retrieves fresh for that utterance. Prevents unbounded context growth and stale KB text across a
   long call; matches RetellAI's invisible inline behavior and 5b's per-reply model.

## 4. Current-state facts (verified)

These are the load-bearing facts the design depends on. Citations are exact at the time of writing.

- **The worker is stateless and HTTP-only.** No SQLAlchemy/asyncpg/psycopg/pgvector and no
  `DATABASE_URL`; its only data dependency is `httpx`. It already calls `apps/api` for config and 17
  tools. (`services/agent/pyproject.toml`, `services/agent/src/usan_agent/api_client.py`.)
- **Worker→api auth.** Shared `JWT_SIGNING_KEY` (HS256). Two token types: a **call-scoped** token
  (`require_service_token`, claim `call_id`, 5-min TTL) used for `/v1/tools/*`, and a **worker**
  token (`require_worker_token`, no `call_id`) used for `/v1/runtime/agent-config`. Plaintext http is
  rejected at startup except for local hosts (`localhost`, `127.0.0.1`, `api`). Same Docker bridge in
  dev and prod; round trip <5ms. (`services/agent/src/usan_agent/api_client.py`,
  `apps/api/src/usan_api/auth.py`.)
- **The LLM runs in the worker** (Gemini via Vertex, `vertexai=True`, ADC, `gcp_project`,
  `vertex_location`). So unlike chat (5b retrieved server-side inside `generate_agent_reply`), voice
  retrieval must happen worker-side and be injected into the turn's chat context before the LLM call.
  (`services/agent/src/usan_agent/pipeline.py`.)
- **The worker has no embedding capability** today (`google-genai` is not a dependency). This design
  does not add one — embedding stays server-side in `apps/api`.
- **Injection hook.** LiveKit Agents 1.x supports a per-turn hook
  `Agent.on_user_turn_completed(turn_ctx, new_message)` — the canonical RAG injection point. The
  worker's factories build a **stock `Agent`** today, so a small `Agent` subclass is introduced to
  override it. The worker already tolerates 100–500ms of inline async work per turn (every tool call
  blocks the turn). (`services/agent/src/usan_agent/pipeline.py`, `check_in.py`.)
- **Org scoping is RLS via the request transaction.** `get_db` sets `app.current_org` to the **default
  org** (`set_config('app.current_org', :org, true)` — transaction-local). The runtime/call plane is
  single-org today (per the multitenancy roadmap; multi-org call plane deferred). The `calls` table is
  **FORCE RLS**, so loading a call outside the current org returns no row → fail-closed 404.
  (`apps/api/src/usan_api/db/session.py`, `tenant_context.py`, migration 0032.)
- **`_authorize_call(call_id, claims, db)`** in `tools.py` is the call-scoped authorization pattern:
  403 if the token's `call_id` ≠ body's, 404 if the call is not visible under the current org. The
  new endpoint mirrors it exactly.
- **`resolve_agent_config(db, *, profile_override, contact_profile_id, direction)`** in
  `repositories/agent_profiles.py` returns `ResolvedAgentConfig | None` whose `.config` is the
  `AgentConfig` — and `AgentConfig.llm.knowledge_base_ids` already exists (added in 5b). This resolver
  is the server-authoritative source of `kb_ids`. `tools.py` already uses it server-side (e.g. for SMS
  templates), so reuse is established.
- **`tools.py` already hosts non-LLM worker operations** (`log_transcript`, `log_metrics`) — they are
  worker-invoked, not LLM-registered. A worker-invoked retrieval endpoint belongs there naturally.
- **5b assets to reuse (exact signatures).**
  - `repositories/knowledge_bases.py`: `search_chunks(db, *, kb_ids: list[uuid.UUID], query_embedding,
    limit, max_distance) -> list[ChunkHit]` (cosine distance via `embedding.cosine_distance(...)`,
    `ORDER BY distance`, `LIMIT`, then Python filter `distance <= max_distance`; empty `kb_ids` → `[]`).
  - `compat/kb_embeddings.py`: `embed_query(text, settings) -> list[float]` (Vertex
    `text-embedding-005`, 768-dim, `task_type="RETRIEVAL_QUERY"`, off-loop).
  - `compat/kb_retrieval.py`: `retrieve_context(db, settings, *, kb_ids: list[str], query) ->
    RetrievedContext(text, hit_count)` — gates flag/`gcp_project`/`kb_ids`/blank-query, decodes
    `kb_ids` (skip undecodable), `embed_query` → `search_chunks` → `_assemble` (greedy fill bounded by
    `kb_retrieval_max_context_chars`, first-chunk-truncate, `\n\n` join); PHI-safe logs (kb_count /
    hits / nearest-rounded only).
  - `settings.py`: `kb_retrieval_enabled` (False), `kb_retrieval_top_k` (5, 1–50),
    `kb_retrieval_max_distance` (0.7, 0.0–2.0), `kb_retrieval_max_context_chars` (8000, ≥1),
    `kb_embedding_model` ("text-embedding-005"), `kb_embedding_location` ("us-central1"),
    `gcp_project`.

## 5. Architecture & data flow

```
VOICE WORKER (services/agent)                 APPS/API (tools plane)
RagAgent.on_user_turn_completed(turn_ctx, new_message)
  └─ gate: worker flag ON + cfg.llm.knowledge_base_ids non-empty
  └─ POST /v1/tools/retrieve_kb_context ───►  _authorize_call (FORCE-RLS, fail-closed 404/403)
       {call_id, query=new_message.text}      resolve_agent_config → kb_ids (server-authoritative)
       (call-scoped JWT, ~3s timeout,          gate: voice flag + gcp_project + kb_ids + query
        best-effort → "" on any error)         retrieve_context(...) [5b, reused, under RLS]
       {context, hit_count}  ◄──────────────   embed_query + search_chunks + _assemble
  └─ if context: append an ephemeral context item to turn_ctx (this generation only)
  └─ LLM generates the spoken turn
```

The server re-derives **both** the org (from the call, under RLS) **and** `kb_ids` (from the resolved
profile). The worker is trusted for neither — it sends only `{call_id, query}`. `search_chunks` under
the call's org context can only ever see that org's chunks; a stray cross-org `kb_id` yields no hits
(fail-closed).

## 6. The new endpoint — `POST /v1/tools/retrieve_kb_context`

Location: `apps/api/src/usan_api/routers/tools.py` (the call-scoped worker→api plane). Not registered
as an LLM tool; the worker calls it directly from the retrieval hook.

- **Auth**: `require_service_token` (call-scoped). Retrieval is **per-turn**, so it needs a rate
  ceiling to bound a runaway/hijacked agent's GCP embed spend — but it must **not** share the
  existing per-call tool ceiling (`tool_call_rate`), or a long multi-turn call would exhaust it and
  start 429-ing legitimate tool calls. The plan pins one of: a **dedicated** per-call retrieval
  ceiling, or exempting retrieval from the tool ceiling. Tracked with
  `@track_tool("retrieve_kb_context")` for metrics consistency (the `@track_tool` decorator is
  orthogonal to the rate ceiling).
- **Request** `RetrieveKbContextRequest`: `{ call_id: uuid.UUID, query: str }`.
- **Flow**:
  1. `call = await _authorize_call(body.call_id, claims, db)` — 403 token-mismatch / 404 cross-org.
  2. Resolve config server-side via the agent-config resolution pattern
     (`call.profile_override`, contact's `agent_profile_id`, `call.direction`) →
     `cfg = resolved.config if resolved else DEFAULT_AGENT_CONFIG`.
  3. `kb_ids = cfg.llm.knowledge_base_ids or []` (encoded strings; server-authoritative).
  4. `retrieved = await retrieve_context(db, settings, kb_ids=kb_ids, query=body.query,
     enabled=settings.kb_retrieval_voice_enabled)`, wrapped in try/except → on any retrieval failure
     return empty (`RetrieveKbContextResponse(context="", hit_count=0)`), PHI-safe logged. The request
     is read-only (no commit, no flushed write), so no savepoint is needed — `get_db` rolls back on
     exception. The call is never broken by a retrieval failure.
- **Response** `RetrieveKbContextResponse`: `{ context: str, hit_count: int }`, serialized with
  `exclude_none`.

### 5b refactor: parameterize the gate flag

`retrieve_context` currently reads `settings.kb_retrieval_enabled` internally. Add an explicit
`enabled: bool` parameter so the flag is the **channel's** responsibility:

- chat (`compat/chat_service.py`) passes `enabled=settings.kb_retrieval_enabled`;
- voice (the new endpoint) passes `enabled=settings.kb_retrieval_voice_enabled`.

This collapses 5b's double-gate (chat checked the flag before calling, and `retrieve_context` checked
it again) to a single channel-owned flag and lets the two channels gate independently. Chat behavior
is otherwise unchanged (the savepoint wrapper in `generate_agent_reply` stays).

## 7. Worker side (`services/agent`)

- **`agent_config.py`** — add `knowledge_base_ids: list[str] | None = None` to the worker's
  `LLMConfig` (forward-compat: old configs without it still validate; the field is silently dropped
  today). This is the only reason the worker can see the bound KBs.
- **New `RagAgent(Agent)` subclass** (`rag_agent.py`) holding `(call_id, kb_ids, settings, enabled)`
  and overriding `on_user_turn_completed(turn_ctx, new_message)`:
  - gate: `enabled` and `call_id` and `kb_ids` (no-op otherwise — zero HTTP when off or unbound);
  - `query = new_message.text_content` (the latest user utterance);
  - `context = await api_client.retrieve_kb_context(call_id, settings, query)` (best-effort);
  - if `context`: append an ephemeral context item to `turn_ctx`
    (`"Knowledge base context:\n" + context + "\n\nUse the above context to answer when relevant."`)
    so it influences only this generation.
- **Factories** (`build_agent` / `build_check_in_agent` / `build_inbound_agent` in
  `pipeline.py` / `check_in.py`) construct `RagAgent` instead of stock `Agent`, threading `call_id`,
  `kb_ids` (from `cfg.llm.knowledge_base_ids`), `settings`, and `enabled`. Tools and instructions are
  unchanged.
- **`api_client.py`** — `retrieve_kb_context(call_id, settings, query) -> str`: mint the call-scoped
  token, POST to `/v1/tools/retrieve_kb_context` with a tight `kb_retrieval_timeout_s` (~3s),
  **best-effort returns `""`** on any HTTP error/timeout/non-200 (mirrors `leave_voicemail`). PHI-safe:
  logs counts only, never the query, the context, or kb_ids.

## 8. Settings (two mirrored flags, both default `False`)

- **`apps/api`** — `kb_retrieval_voice_enabled: bool = Field(default=False,
  alias="KB_RETRIEVAL_VOICE_ENABLED")`. The **real egress gate** (ship-inert: no query embed until set
  **and** `gcp_project` set). Reuses 5b's `kb_retrieval_top_k` / `kb_retrieval_max_distance` /
  `kb_retrieval_max_context_chars` and the embedding model/location — one tuning set for both channels.
- **`services/agent`** — `kb_retrieval_voice_enabled: bool = Field(default=False,
  alias="KB_RETRIEVAL_VOICE_ENABLED")` and `kb_retrieval_timeout_s: float` (default ~3.0). The worker
  flag gates whether the per-turn HTTP call happens at all, so the feature is a true no-op when off
  (no wasted round-trip per turn).
- **Activation** = both flags + `gcp_project`, present in **both** services' compose `environment:`
  maps **and** the VM `.env` (the `compose-env-passthrough` two-place rule; a new key silently no-ops
  otherwise).

## 9. Error handling, latency, PHI

- **Never breaks the call.** Two independent best-effort layers: the worker client returns `""` on any
  failure, and the endpoint degrades to empty on any retrieval failure. A failure → the turn proceeds
  with no injected context, exactly as if no KB were bound.
- **Latency.** Retrieval runs pre-LLM inside the turn hook: embed (~50–200ms) + <5ms hop, capped by
  `kb_retrieval_timeout_s`. Turn-taking already tolerates this (every tool call blocks the turn). The
  worst-case gap is the timeout; documented as a tunable. Known limitation: a cold/slow embed adds a
  perceptible gap before the agent speaks; the tight timeout bounds it.
- **PHI / secrets.** Counts + bucketed/rounded distances only, ever (reuse 5b's `retrieve_context`
  logging). The query text, the retrieved context, source titles, and kb_ids are **never** logged on
  either side. RLS is the hard isolation guarantee; `organization_id` is server-set, never by app
  code. Context travels the same authenticated internal/https channel as all other PHI (config, tool
  payloads). `exclude_none` on the response.

## 10. Testing matrix

- **`apps/api`** (new endpoint):
  - bound kb_ids + voice flag on + gcp_project → context returned (`hit_count > 0`);
  - voice flag off → empty; no kb_ids bound → empty; blank query → empty;
  - token not scoped to the call → 403; call not visible under the org → 404;
  - retrieval raises (DB-class) → endpoint returns empty 200, does not 500.
- **`apps/api`** (`retrieve_context` `enabled`-param refactor): chat path still gated by
  `kb_retrieval_enabled` (5b tests updated to pass `enabled=`); voice path gated by the voice flag.
- **`services/agent`**:
  - `LLMConfig` now parses `knowledge_base_ids`; an old config without the field still validates
    (forward-compat);
  - `RagAgent.on_user_turn_completed`: injects context into `turn_ctx` on a hit; no-ops when disabled
    / no call_id / no kb_ids / empty context / retrieval error;
  - `api_client.retrieve_kb_context`: returns `""` on HTTP error/timeout/non-200; never logs
    query/context/kb_ids.
  - Mock the LLM/session per the existing `AsyncMock`/`MagicMock`/`monkeypatch` patterns.

## 11. Out of scope

- Voice-specific retrieval tunables (top-k / max-distance / context-size) — reuse the shared 5b
  settings for v1. Voice answers are short and the context cap is a ceiling, not a target; a
  voice-specific smaller cap is a noted follow-up, not v1.
- Re-ranking, cross-turn embedding cache, streaming retrieval.
- Multi-org call plane (deferred program-wide; this endpoint inherits the tool plane's single-org RLS
  posture — no better, no worse — and upgrades with it).
- Any chat behavior change beyond the `enabled`-param refactor.

## 12. Files touched

- **`apps/api`**: `routers/tools.py` (new endpoint), `compat/kb_retrieval.py` (`enabled` param),
  `compat/chat_service.py` (pass `enabled`), `settings.py` (voice flag),
  `compat/schemas/` (request/response models), tests under `tests/` + `tests/compat/`.
- **`services/agent`**: `agent_config.py` (`knowledge_base_ids`), new `rag_agent.py` (the subclass),
  `pipeline.py` / `check_in.py` (factories build `RagAgent`), `api_client.py`
  (`retrieve_kb_context`), `settings.py` (voice flag + timeout), tests under `tests/`.
- **`docs/deployment/voice-rag-retrieval.md`** (operator note: served behavior, ship-inert +
  two-flag activation order, the shared tunables, VERIFY-at-deploy, PHI/security, known limitations).

## 13. Invariants (carried from the program)

- No new alembic migration — `kb_ids` rides the existing config JSONB; single head stays `0047`.
- Ships **inert** (both `kb_retrieval_voice_enabled` default `False`; no `v*` tag this phase).
- `KNOWN_GAPS` stays `frozenset()`.
- `apps/api` and `services/agent` never import each other (the HTTP boundary is the only link).
- CI mypy = `uv run mypy` (config `files=["src"]`) — never `mypy .`. ruff line-length 100,
  target py314 (api) / py312 (agent). pytest runs `-n auto` (parallel) — cross-org/global-state tests
  must tolerate sibling rows. Verify `except (A, B):` syntax via `python -m py_compile`, not by eye.
- `AgentConfig` is frozen + `extra="ignore"`, re-validated on every read — any new field must be
  Optional with a default (forward-compat). `knowledge_base_ids` on the worker's `LLMConfig` satisfies
  this.
