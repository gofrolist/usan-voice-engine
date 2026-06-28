# RetellAI Parity Phase 4c-2 — rerun-chat-analysis + Vertex chat_analysis pipeline

**Date:** 2026-06-27
**Program:** RetellAI full-API-parity (any client repoints base URL with zero changes)
**Builds on:** Phase 4a (api_chat `chat_sessions`/`chat_messages`, MERGED), 4b (two-way SMS, MERGED), 4c-1 (chat-agent CRUD, MERGED, mig 0045)
**Ships as:** its own squash-merged PR. **No `v*` tag.** Migration `0046` inert until an operator deploys.

---

## 1. Goal

Serve RetellAI's **`PUT /rerun-chat-analysis/{chat_id}`** compat operation (the last unserved chat op), backed by a real **Vertex-driven post-chat analysis pipeline** that mirrors the existing `summarization.py` call summarizer. The op recomputes a chat's post-chat analysis on demand and returns the chat with the fresh `chat_analysis` populated. Adding `chat_analysis` to the shared `CompatChat` serializer also surfaces stored analysis on `get-chat` and `list-chats`.

This is the second half of Phase 4c (4c-1 was chat-agent CRUD). The earlier decomposition deferred the analysis pipeline here so the rerun op delivers **real analysis**, not a frozen-null stub.

---

## 2. Oracle contract (vendored `openapi-final.yaml` v3.0.0)

- **Operation:** `rerunChatAnalysis` — method **PUT**, path **`/rerun-chat-analysis/{chat_id}`**, path param `chat_id` (string, required), **no request body**, success **201**, response `#/components/schemas/ChatResponse`. Error responses: 400/401/402/404/422/429/500.
- **`ChatResponse`** net-required fields: `chat_id`, `agent_id`, `chat_status`. 12 optional fields including `chat_analysis: $ref ChatAnalysis`. Shared by 5 ops (createChat 201, createSmsChat 200, getChat 200, updateChatMetadata 200, rerunChatAnalysis 201).
- **`ChatAnalysis`** (no required fields, all optional):
  - `chat_summary` — string
  - `user_sentiment` — enum `Negative | Positive | Neutral | Unknown` (title-case)
  - `chat_successful` — boolean
  - `custom_analysis_data` — arbitrary object
- **retell-sdk 5.53.0:** `retell.types:ChatResponse` (`extra='allow'`); `ChatAnalysis` lives at `retell.types.chat_response` (not a package-level export, but `ChatResponse.chat_analysis` types it, so round-tripping `ChatResponse` validates it). Conformance harness uses `assert_conforms(body, "ChatResponse")` + `assert_sdk_roundtrip(body, "retell.types:ChatResponse")`.

**Conformance discipline:** `CompatChat` uses `| None = None` + the routes set `response_model_exclude_none=True`, so absent fields are omitted (not serialized as `null`). `user_sentiment` must be one of the four exact enum strings or absent — the serializer coerces any off-enum/garbage Vertex value to `None`.

---

## 3. Design decisions (locked)

1. **Storage = new `chat_analyses` table** (mirrors the call analog `conversation_summaries`): a dedicated TenantScoped + RLS table with discrete columns, `chat_session_id` UNIQUE for upsert idempotency, and `model_version` for per-run audit. Keeps analysis off the hot session row.
2. **Analysis depth = full ChatAnalysis via Vertex:** the LLM produces `chat_summary` + `user_sentiment` (enum) + `chat_successful` (judgment of whether the agent met the user's goal). `custom_analysis_data` is **deferred** (always null this phase — agent-defined extraction is a future phase). This is richer than the call summarizer (which freezes `user_sentiment` null and derives `call_successful` from status); the chat channel has no clean status→success mapping, so success is an LLM judgment.
3. **Trigger scope = rerun op only (inline):** implement just the PUT endpoint, which runs the pipeline **inline** (await) and returns 201 with the fresh analysis. The pipeline core is built reusable. **Deferred:** auto-analyze-on-end-chat, the `chat_analyzed` webhook (`lifecycle.py` chat path), agent-defined `custom_analysis_data`.

---

## 4. Architecture

```
PUT /rerun-chat-analysis/{chat_id}                  (compat/routers/chats.py — existing router)
  └─ chat_service.rerun_chat_analysis(db, settings, chat_id)
       ├─ decode chat_id → session UUID          (404 on bad id)
       ├─ chats_repo.get_session(db, sid)         (404 if missing/archived; RLS → cross-org = 404)
       ├─ chat_analysis.analyze_chat_with(db, sid, settings, force=True)   ── inline Vertex ──┐
       │     ├─ gate: flag off / no gcp_project → return existing record (no Vertex)          │
       │     ├─ chats_repo.list_messages(db, sid) → render capped transcript                  │
       │     ├─ run_vertex_turn(model=chat_analysis_model, tools=[], …)   (reused, ADC/Vertex) │
       │     ├─ defensive JSON parse → {chat_summary, user_sentiment(coerced), chat_successful}│
       │     └─ chat_analyses_repo.upsert(...)    (INSERT … ON CONFLICT(chat_session_id) DO …) │
       ├─ chats_repo.list_messages(db, sid)       (for transcript serialization)              │
       ├─ chat_serializer.serialize_chat(session, messages, include_transcript=True,          │
       │                                  analysis=record)                                     │
       ├─ _audit(request, "rerun-chat-analysis", chat_id)   (PHI-free: org+op+id only)         │
       └─ commit → 201 CompatChat {…, chat_analysis}  ◄──────────────────────────────────────┘
```

`get_compat_db` yields the RLS-org-scoped `AsyncSession` (sets `app.current_org`); every query is org-isolated by the TenantScoped column + RLS policy.

---

## 5. Components

### 5.1 Migration `0046_chat_analyses.py` + ORM `ChatAnalysisRecord`
- `revision="0046"`, `down_revision="0045"`. Owner-DDL (deploy migrates as `usan` owner).
- `CREATE TABLE chat_analyses` with the columns in §1; `organization_id` default `COALESCE(current_setting('app.current_org', true)::uuid, default_org_id())`.
- `ENABLE` + `FORCE ROW LEVEL SECURITY`, `CREATE POLICY tenant_isolation … USING/WITH CHECK (organization_id = current_setting('app.current_org', true)::uuid)`, `GRANT SELECT, INSERT, UPDATE, DELETE ON chat_analyses TO usan_app`.
- `UNIQUE (chat_session_id)`; FK `chat_session_id → chat_sessions(id) ON DELETE CASCADE`.
- `downgrade()` drops the table.
- ORM `ChatAnalysisRecord(TenantScoped, Base)` in `db/models.py` (table `chat_analyses`), named distinctly from the pydantic `ChatAnalysis`.

### 5.2 Repository `repositories/chat_analyses.py`
- `get_for_session(db, session_id) -> ChatAnalysisRecord | None`
- `get_for_sessions(db, session_ids) -> dict[UUID, ChatAnalysisRecord]` — batched loader for the list path (one `WHERE chat_session_id IN (…)` query → no N+1).
- `upsert(db, session_id, *, chat_summary, user_sentiment, chat_successful, custom_analysis_data, model_version) -> ChatAnalysisRecord` — `INSERT … ON CONFLICT (chat_session_id) DO UPDATE` setting all analysis columns + `updated_at`. **Never sets `organization_id`** (TenantScoped DB default + RLS `WITH CHECK`).

### 5.3 Pipeline `chat_analysis.py` (mirrors `summarization.py`)
- `analyze_chat_with(db, session_id, settings, *, force=False) -> ChatAnalysisRecord | None`:
  - **Gate:** `if not settings.chat_analysis_enabled or not settings.gcp_project:` return `get_for_session(...)` (existing or None) — no Vertex call.
  - **Idempotency:** if `not force` and a record exists, return it untouched.
  - Load messages; if the session has zero messages, return `get_for_session(...)` without a Vertex call (nothing to analyze).
  - Render transcript: `"\n".join(f"{m.role}: {m.content}")`, capped to `_MAX_TRANSCRIPT_CHARS` (12000, matching the summarizer).
  - `run_vertex_turn(model=settings.chat_analysis_model, temperature=0.2, system_instruction=_SYSTEM_INSTRUCTION, tools=[], contents=[{"role": "user", "parts": [{"text": transcript}]}], settings=settings)`.
  - Parse via `_parse_analysis(text)`: strip code fences, `json.loads`, on failure fall back to `{chat_summary: raw[:cap]}`. Extract:
    - `chat_summary`: str, truncated to `_MAX_SUMMARY_CHARS` (4000).
    - `user_sentiment`: coerced — title-cased and checked against `_VALID_SENTIMENTS = frozenset({"Negative","Positive","Neutral","Unknown"})`; anything else → `None`.
    - `chat_successful`: `bool` if the parsed value is a real bool, else `None`.
  - `custom_analysis_data = None` (deferred).
  - `upsert(...)` with `model_version=settings.chat_analysis_model`.
  - **Error handling:** wrap the Vertex+parse+upsert body in `try/except Exception` → log `type(exc).__name__` only (no PHI), return `get_for_session(...)` (prior record or None). Never raises — the inline endpoint stays 201.
- `_SYSTEM_INSTRUCTION` (verbatim intent): instruct the model to return ONLY a JSON object with keys `chat_summary` (1-3 sentence recap, warm and factual), `user_sentiment` (one of `Positive`, `Negative`, `Neutral`, `Unknown`), and `chat_successful` (boolean: whether the agent accomplished the user's goal). "Do not invent details."

### 5.4 Compat schema + serializer
- `compat/schemas/chats.py`: add
  ```python
  class ChatAnalysis(BaseModel):
      chat_summary: str | None = None
      user_sentiment: str | None = None
      chat_successful: bool | None = None
      custom_analysis_data: dict[str, Any] | None = None
  ```
  and `chat_analysis: ChatAnalysis | None = None` on `CompatChat`.
- `chat_serializer.serialize_chat(session, messages, *, include_transcript, analysis: ChatAnalysisRecord | None = None) -> CompatChat`: when `analysis is not None`, build `ChatAnalysis(chat_summary=record.chat_summary, user_sentiment=record.user_sentiment, chat_successful=record.chat_successful, custom_analysis_data=record.custom_analysis_data)` and set `chat_analysis`; else leave `None` (omitted by `exclude_none`). The serializer stays pure/sync (no db) and does a **straight pass-through** — sentiment coercion happens **once, at write time** in the pipeline (§5.3), so the stored `user_sentiment` is always a valid enum value or `NULL`; the serializer does not re-coerce.

### 5.5 Service `compat/chat_service.py`
- `rerun_chat_analysis(db, settings, chat_id) -> CompatChat`: decode → `get_session` (raise `CompatError(404, "chat not found")` if `None`) → `analyze_chat_with(db, sid, settings, force=True)` → `list_messages` → `serialize_chat(..., include_transcript=True, analysis=record)`. Mirrors the existing `get_chat` load/serialize structure.
- Extend `get_chat`: load `chat_analyses_repo.get_for_session` and pass `analysis=` to the serializer.
- Extend `list_chats`: `get_for_sessions(db, [s.id for s in sessions])` once; pass each session's record (or `None`) to the serializer.
- `create_chat`/`create_sms_chat`/`create_chat_completion`: brand-new chats have no record → `analysis=None` (omitted). No behavior change; existing freeze tests unaffected.

### 5.6 Router + unsupported + settings
- `compat/routers/chats.py`: add `@router.put("/rerun-chat-analysis/{chat_id}", status_code=201, response_model=CompatChat, response_model_exclude_none=True)`; thin handler → `chat_service.rerun_chat_analysis` → `_audit` → return. Settings injected the same way the existing create/get handlers receive them.
- `compat/routers/unsupported.py`: remove the single line `("PUT", "/rerun-chat-analysis/{chat_id}")`. **Leave `("PUT", "/rerun-call-analysis/{call_id}")`** (still unsupported).
- `settings.py`: `chat_analysis_enabled: bool = False` (`CHAT_ANALYSIS_ENABLED`), `chat_analysis_model: str = "gemini-2.5-flash"` (`CHAT_ANALYSIS_MODEL`).

---

## 6. Data flow & states

| State | `chat_analysis` in response |
|---|---|
| New chat, never analyzed | omitted (no record) |
| Flag off / no `gcp_project`, rerun called | omitted if no prior record; prior record echoed if one exists — graceful no-op, **201** |
| Flag on, rerun called | freshly computed, upserted, returned |
| Vertex/parse error during rerun | prior record echoed (or omitted); **201** (never 5xx) |
| `get-chat` / `list-chats` after a rerun | stored record reflected (single load / batched load) |

---

## 7. Security, PHI, tenancy

- `chat_analyses` is TenantScoped + FORCE RLS → org isolation; `upsert` never sets `organization_id` (DB default + RLS `WITH CHECK`).
- A cross-org `chat_id` resolves to a row the RLS session can't see → `get_session` returns `None` → **404** (no existence leak).
- `_audit` logs **org id + op + chat_id only** — never transcript, summary, sentiment, prompt, or numbers.
- Pipeline errors log the **exception type name only**. The transcript egresses **only to Vertex via ADC** (BAA-covered), never to the Gemini Dev API; analysis persists only to BAA Postgres.
- **No voice/chat leak surface:** the op reads a chat *session*, not an agent profile. `session.agent_profile_id` already references a chat agent, so no 4c-1-style channel-seal work is needed (noted so the reviewer knows it was considered, not skipped).

---

## 8. Testing (TDD)

- **Migration/table:** `chat_analyses` exists, has the columns, RLS is enabled+forced, `usan_app` is granted (DDL/structure test).
- **Repo:** `upsert` insert-then-update yields exactly one row with updated values; `get_for_session`; `get_for_sessions` batched dict; cross-org RLS isolation (org B can't read/write org A's analysis).
- **Pipeline:** JSON parse (clean, code-fenced, garbage→fallback); `user_sentiment` coercion (off-enum → `None`, case normalization); `chat_successful` non-bool → `None`; `force=True` overwrites, `force=False` returns existing untouched; flag-off no-op (no Vertex call, returns existing/None); zero-message no-op; Vertex-raises → swallowed, returns prior record; transcript cap. Vertex mocked via `monkeypatch` of `chat_analysis.run_vertex_turn`.
- **Freeze:** `tests/compat/test_freeze_chat_analysis.py` — `@pytest.mark.frozen`, mocked Vertex returning a known JSON; create a chat, append a turn, `PUT /rerun-chat-analysis/{chat_id}`; assert 201, `chat_analysis` present with `chat_summary`/`user_sentiment`/`chat_successful`, `assert_conforms(body, "ChatResponse")`, `assert_sdk_roundtrip(body, "retell.types:ChatResponse")`.
- **Router/behavior:** 401 without a compat key; 404 unknown chat; 404 archived chat; rerun populates analysis; subsequent `get-chat` reflects it; `list-chats` reflects it (batched, no N+1); cross-org rerun → 404.
- **Coverage:** `test_surface_coverage` still passes with `KNOWN_GAPS == frozenset()` once the route is served and the unsupported entry removed; `test_501_stub_paths_match_oracle_exactly` still passes.
- **Docs:** `docs/deployment/chat-analysis.md` — operator note (env keys, inert-by-default, BAA/Vertex requirement).

---

## 9. Global constraints (carried from the program)

- `apps/api` and `services/agent` never import each other.
- `organization_id` is server-set (RLS); app code never assigns it.
- PHI/secret-safe logging only (`_audit` = org id + op + id; pipeline errors = exception type only).
- `exclude_none` on every serialized response (a serialized `null` key fails the pinned oracle).
- `KNOWN_GAPS` stays `frozenset()`.
- Served paths use the oracle's exact path strings.
- CI mypy = `uv run mypy` (config `files=["src"]`) — never `mypy .`. CI also runs ruff + the full pytest (`-n auto`).
- Commit scope `api`; attribution disabled (no `Co-Authored-By`, no 🤖 footer).
- Squash-merge to protected `main` only on explicit go-ahead. **No `v*` tag** — migration `0046` inert until an operator deploys + mints a compat key.

---

## 10. Out of scope (clean follow-ups)

- Auto-analyze on chat end (fire-and-forget on `status→ended`, mirroring `summarize_call`).
- The `chat_analyzed` webhook — extend `lifecycle.py` with `enqueue_compat_chat_event` (the oracle defines the event; `lifecycle.py` currently has the call path only).
- Agent-defined `custom_analysis_data` extraction (consume the chat-agent's `post_chat_analysis_data` schema).
- `rerun-call-analysis` (the voice analog) stays unsupported.
