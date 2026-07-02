# RetellAI Parity — Phase 7 slice 2: `v2/list-retell-llms` + `rerun-call-analysis`

**Date:** 2026-07-01
**Status:** Approved
**Program:** RetellAI full-API parity (roadmap: `2026-06-24-retell-full-parity-program-roadmap.md`)
**Prior slice:** Phase 7 slice 1 `agent-playground-completion` (#155, squash 36d6342)

## 1. Goal & scope

Move two 501 stubs to served, keeping `KNOWN_GAPS = frozenset()`:

1. `GET /v2/list-retell-llms` — the paginated v2 list of Retell-LLM response engines.
2. `PUT /rerun-call-analysis/{call_id}` — recompute a call's post-call analysis and
   return the full call object (201).

Same program posture as every prior slice: merged-not-deployed (needs a v* tag);
`rerun-call-analysis` makes a Vertex call only when `summarization_enabled` +
`gcp_project` are set (ship-inert, mirroring the 4c-2 chat-analysis gate). One
additive/inert migration (0051). No new env keys.

Out of scope: the remaining Phase 7 stubs (voice cloning, get-mcp-tools,
create-agent-version, Test Suites/Sim, create-phone-number), the deferred
compat-CRUD dedup refactor.

## 2. Oracle shapes (authoritative)

- `GET /v2/list-retell-llms` → `listRetellLLMV2`. Query params (all optional):
  `limit` (integer, **default 50**, max 1000), `sort_order`
  (`ascending|descending`, default `descending`), `pagination_key` (string).
  200 body = `PaginatedResponseBase` (`has_more: bool`, `pagination_key: string`)
  + `items: RetellLLMResponse[]`. SDK round-trip target:
  `retell.types.LlmListResponse` (concrete: `items`/`has_more`/`pagination_key`).
- `PUT /rerun-call-analysis/{call_id}` → `rerunCallAnalysis`. Path param
  `call_id`. **201** body = `V2CallResponse` (the oneOf; serialize the concrete
  variant per row: phone → `V2PhoneCallResponse` / SDK `PhoneCallResponse`, web →
  `V2WebCallResponse`). `call_analysis` = `CallAnalysis`
  (`call_summary`, `call_successful`, `in_voicemail`, `user_sentiment`,
  `custom_analysis_data`). Oracle documents no 422 "nothing to analyze" mode; the
  op's contract is "analysis rerun successfully" + the `call_analyzed` webhook.

The existing root `GET /list-retell-llms` (bare array, limit default 1000) is a
separate frozen op and is **untouched** (pinned by
`test_list_retell_llms_is_bare_array_at_root`).

## 3. Design decisions (user-approved)

1. **Rerun writes into `conversation_summaries` (upsert), facts skipped.** The
   compat `call_analysis` is already read from this product table
   (`call_serializer._build_analysis`); a rerun genuinely replaces the call's
   summary row, which also freshens the next-call `last_call_summary`/`open_plans`
   built-ins. `personal_facts` extraction is **skipped on rerun** so facts never
   duplicate. No parallel compat table (rejected: two sources of truth, double
   Vertex spend); no in-memory-only recompute (rejected: next get-call would show
   stale analysis — observably non-conformant).
2. **Best-effort 201, mirroring `analyze_chat_with`.** 404 only for
   missing/archived call. Unconfigured (flag/gcp unset), no transcript, or a
   Vertex/parse failure → return the call's current state, still 201. Contact-less
   web calls are covered: the recompute relaxes the `contact_id` requirement under
   force (migration 0051 makes the column nullable).
3. **Real keyset pagination for the v2 list**, a structural port of 6a's
   `GET /v2/list-conversation-flows` (base64 `created_at|id` cursor, lenient
   bad-cursor → first page, `limit+1` fetch). Not the single-page
   v2-list-agents shortcut. The shared-cursor-helper dedup refactor stays
   deferred (6b decision): port the pattern, don't abstract yet.

## 4. Op 1 — `GET /v2/list-retell-llms`

- **Router** (`compat/routers/retell_llm.py`): new GET handler.
  `sort_order: Literal["ascending","descending"] = Query(default="descending")`,
  `limit: int = Query(default=50, ge=1, le=1000)`,
  `pagination_key: str | None = Query(default=None)`. Unparseable cursor →
  `contextlib.suppress(CompatError)` → first page.
- **Cursor codec** (`compat/ids.py`): `encode_retell_llm_cursor(created_at, id)` /
  `decode_retell_llm_cursor(key)` — same scheme as the conversation-flow cursor.
- **Repo** (`repositories/agent_profiles.py`): a keyset-capable list query —
  archived excluded, `channel=None` (channel-agnostic per 4c-1: both voice and
  chat profiles appear, matching the root list), order by `(created_at, id)` in
  either direction, `after` keyset tuple, fetch `limit+1`.
- **Response**: `{"items": [agent_bridge.serialize_llm(p).model_dump() ...],
  "has_more": bool}` + `"pagination_key"` **only when** `has_more`
  (RetellAI omit-nulls).
- **Surface**: remove the `/v2/list-retell-llms` stub from `unsupported.py`;
  update **both** surface-coverage test files
  (`tests/compat/test_surface_coverage.py` + `tests/test_compat_fidelity.py`).

## 5. Op 2 — `PUT /rerun-call-analysis/{call_id}`

**Flow** (handler in `compat/routers/calls.py`):
`_load_call(call_id)` (404 missing/archived; RLS scopes the lookup so a
cross-org id is a clean 404) → force recompute (below) → `await db.commit()` →
`serialize_call(...)` with the same options as get-call → **201**.

**`summarization.py` changes** — `summarize_call_with(db, call_id, settings, *,
force: bool = False)`:

- `force=True` skips the already-summarized early return and **upserts** the
  summary row: new repo function `conversation_summaries.upsert(...)` via
  `ON CONFLICT (call_id) DO UPDATE SET summary, open_plans, model_version`
  (insert-or-replace; the existing insert-only `create` keeps its
  DO-NOTHING semantics for the background trigger).
- `force=True` relaxes the `contact_id is None` bail so contact-less web calls
  recompute; the row persists `contact_id = NULL`. `get_latest(contact_id=…)`
  filters by contact, so NULL rows never feed built-ins — safe by construction.
- `force=True` **skips persisting extracted facts** (`personal_facts` writes);
  the Vertex prompt and parse are unchanged, parsed facts are dropped.
- The existing `enqueue_compat_call_event(db, call, event="call_analyzed")`
  inside `summarize_call_with` fires again on rerun — **intentional and
  oracle-faithful** (clients subscribe to `call_analyzed` to receive the redone
  analysis).
- **Commit ownership:** today `summarize_call_with` commits internally (the
  background trigger owns its session). Under `force=True` it is **flush-only**
  — the compat router commits — mirroring `analyze_chat_with`'s contract, so
  the enqueue + summary upsert land in the request's transaction. Behavior at
  `force=False` is byte-identical to today (background trigger untouched,
  still commits internally).

**Best-effort wrapper** (compat side, mirrors `analyze_chat_with`'s
never-raises contract):

- Unconfigured (`not (settings.summarization_enabled and settings.gcp_project)`)
  → no Vertex call, no write, return current state.
- No transcript rows → no Vertex call, return current state (existing behavior
  of `summarize_call_with`).
- Vertex/parse failure → roll back the recompute's uncommitted work, log
  `type(exc).__name__` only (PHI-safe), return the prior state.
- All of the above answer **201** with the serialized call. This op never
  returns 422/503.

**Migration 0051**: `ALTER conversation_summaries.contact_id DROP NOT NULL`.
Additive/inert, owner-DDL (the deploy path already migrates as the `usan`
owner — see the shipped owner-migration fix). Alembic stays single-head (0051).

## 6. Error handling & security

- 404s use the house `CompatError` envelope (documented oracle deviation,
  unchanged program-wide posture).
- PHI containment: transcript/summary text is never logged (type-only errors,
  `call_id` + counts); the transcript goes only to Vertex via `vertexai=True` +
  ADC (BAA) — never the Gemini Developer API. No new log lines carry E.164 or
  transcript text.
- Both ops are compat-key-authed and RLS-scoped like the rest of the surface.
  Each rerun is one bounded Vertex turn (transcript capped at
  `_MAX_TRANSCRIPT_CHARS`), the same cost class as create-chat-completion; no
  new rate-limit machinery.

## 7. Testing

Frozen conformance (`@pytest.mark.frozen`):

- List page vs the oracle (`PaginatedResponseBase` + `RetellLLMResponse[]`) +
  SDK round-trip vs `retell.types.LlmListResponse`; `pagination_key` omitted on
  the last page.
- Rerun 201 body vs the oracle + SDK round-trip (`PhoneCallResponse` for phone
  rows; the web branch for web rows).

Behavior:

- Keyset walk: 2 pages both sort orders, `has_more`/cursor continuity, bad
  cursor → first page, archived excluded, both channels present.
- Rerun: upsert replaces summary/open_plans/model_version; facts NOT
  re-persisted on force; contact-less web call persists a NULL-contact row and
  built-ins ignore it; unconfigured / no-transcript / Vertex-failure → 201 with
  prior state and no partial writes; archived/missing/cross-org → 404;
  `call_analyzed` re-enqueued on rerun; background path (`force=False`)
  unchanged (existing tests keep passing).
- Both surface-coverage files updated for the two 501→served moves.

Gate: full `apps/api` suite green, `mypy` + `ruff` clean, alembic single head
0051.
