# RetellAI Parity Phase 6a — Conversation-Flow CRUD (design)

**Date:** 2026-06-30 · **Program:** RetellAI Full API Parity · **Phase:** 6a (first sub-phase of Phase 6 — Conversation Flow) · **Status:** Approved — ready for the implementation plan

> Parent program roadmap: `docs/superpowers/specs/2026-06-24-retell-full-parity-program-roadmap.md` (§4 Phase 6).
> Each phase ships as its own `spec → plan → implementation` cycle and its own squash-merged PR. Merged ≠ deployed (gated by a `v*` tag).

---

## 1. Context & Goal

Phase 6 of the program is **Conversation Flow** — RetellAI's visual flow-builder agent type: a response engine that drives a call/chat through a DAG of typed nodes. The roadmap rates the full phase **XL** ("the heaviest build… no vendor provides Retell's flow semantics"). The oracle surface is **10 operations** (5 conversation-flow + 5 conversation-flow-component), all currently served as **correct-path 501 stubs** (so `KNOWN_GAPS` is already `frozenset()`).

Following the program's established discipline — **cheapest-parity-first → greenfield-last**, and **CRUD-and-conformance before runtime** (exactly how Phase 2 phone-numbers shipped "bindings persisted-not-honored", Phase 3 web-calls shipped "agent_override persisted-not-honored", and Phase 5 shipped KB management before 5b/5c retrieval) — Phase 6 is **decomposed** into:

- **6a — Conversation-Flow CRUD** *(this spec)* — the 5 flow ops, persisted-not-honored, conformance-frozen. Clears the 5 flow stubs.
- **6b — Conversation-Flow Component CRUD** — the 5 component ops (incl. the delete→"local copies in linked flows" side-effect). Deferred.
- **6c — Agent ↔ conversation-flow binding fidelity** — validate + cleanly echo `response_engine.type="conversation-flow"` on agents (fixes the `serialize_agent` hard-code). Deferred.
- **6-runtime** — the greenfield DAG execution engine (**chat-first, then voice** — see §12). The genuine XL; its own future track.

**Goal of 6a.** A RetellAI client can create / get / update / delete / list **conversation flows** against our service at the exact oracle paths and shapes, with full conformance freeze. The flow is **persisted-and-echoed but not executed** at call/chat time (no runtime in 6a). 6a is **purely additive**: a new table, new id-prefix, new service + router, new tests — **zero edits to agent / LLM / worker code**.

---

## 2. Scope

**In scope (6a):**
- `POST /create-conversation-flow` → 201 `ConversationFlowResponse`
- `GET /get-conversation-flow/{conversation_flow_id}?version` → 200 `ConversationFlowResponse`
- `PATCH /update-conversation-flow/{conversation_flow_id}?version` → 200 `ConversationFlowResponse`
- `DELETE /delete-conversation-flow/{conversation_flow_id}` → 204 (empty body)
- `GET /v2/list-conversation-flows` → 200 paginated (`items` + `has_more` + `pagination_key`)
- New `conversation_flows` table (migration 0048, FORCE-RLS, TenantScoped).
- New `conversation_flow_<hex>` id-codec prefix.
- Frozen conformance + CRUD/RLS behavior tests; remove the 5 flow stubs from `unsupported.py`.

**Out of scope (deferred):**
- The 5 **conversation-flow-component** ops — stay 501 (→ **6b**).
- **Agent binding** of `response_engine.type="conversation-flow"` + the `serialize_agent` fidelity fix + cross-org `conversation_flow_id` validation (→ **6c**).
- **Any runtime/execution** of a flow on a voice call or chat turn (→ **6-runtime**).
- **Full version history** / `?version` historical reads (see §8 — current-only by decision).
- Deep per-node / per-edge / model-enum **semantic validation** (accept-and-echo by decision — see §6).

---

## 3. Locked Decisions (from brainstorming, 2026-06-30)

1. **First PR scope = Flow CRUD only (5 ops)**, persisted-not-honored, purely additive. Components / binding / runtime are later sub-phases.
2. **Versioning = current-only mutable row.** One row per flow; `version` int starts at 0 and bumps on each update; `?version` accepted but always serves current (documented persisted-not-honored posture). No version-history table.
3. **Binding fidelity → separate 6c sub-phase** (keep 6a from touching the frozen agent serializer).

---

## 4. Oracle Facts (authoritative shapes)

Oracle: vendored `apps/api/tests/compat/oracle/openapi-final.yaml` (v3.0.0); SDK pin `retell-sdk==5.53.0`.

### 4.1 Operations (all 5 flow ops; paths exact)
| Method | Path | Req schema | Resp schema | Status |
|---|---|---|---|---|
| POST | `/create-conversation-flow` | `CreateConversationFlowRequest` | `ConversationFlowResponse` | **201** |
| GET | `/get-conversation-flow/{conversation_flow_id}` (`?version` int) | — | `ConversationFlowResponse` | 200 |
| PATCH | `/update-conversation-flow/{conversation_flow_id}` (`?version` int) | `ConversationFlow` | `ConversationFlowResponse` | 200 |
| DELETE | `/delete-conversation-flow/{conversation_flow_id}` | — | empty | **204** |
| GET | `/v2/list-conversation-flows` (`limit`,`sort_order`,`pagination_key`) | — | `PaginatedResponseBase` + `{items: ConversationFlowResponse[]}` | 200 |

### 4.2 Schemas
- **`ConversationFlow`** = `allOf [ConversationFlowOverride, inline]`. **Every field optional** (no `required` block).
  - From `ConversationFlowOverride`: `model_choice` (`$ref ModelChoice`), `model_temperature` (number 0..1, nullable), `tool_call_strict_mode` (bool, nullable), `knowledge_base_ids` (string[], nullable), `kb_config` (`$ref KBConfig` — see quirk in §7.4), `start_speaker` (enum `[user, agent]`), `begin_after_user_silence_ms` (int, nullable).
  - From the inline block: `global_prompt` (string, nullable), `flex_mode` (bool, nullable), `tools` (`NodeTool[]`, nullable), `components` (`CreateConversationFlowComponentRequest[]`, nullable — inline local components ride in the flow body for free), `start_node_id` (string, nullable), `default_dynamic_variables` (object<string,string>, nullable), `begin_tag_display_position` (`{x:number, y:number}`, nullable), `notes` (`Note[]`, nullable), `mcps` (`MCP[]`, nullable), `is_transfer_llm` (bool, nullable), `nodes` (`ConversationFlowNode[]`).
  - **There is no `name` field** on a flow (only components have `name`). Flows are identified solely by `conversation_flow_id`.
- **`ConversationFlowResponse`** = `allOf [ConversationFlow, {required: conversation_flow_id (string), version (int), last_modification_timestamp (int, ms epoch)}]`. All three **server-generated**, non-nullable. No other added fields.
- **`CreateConversationFlowRequest`** = `allOf [ConversationFlow, {required: [start_speaker, model_choice, nodes]}]` — the only three fields required on create.
- **`ModelChoice`** = `oneOf [ModelChoiceCascading]`; `ModelChoiceCascading` = `{required: type=="cascading", model: <LLMModel enum>, high_priority?: bool}`. The `LLMModel` enum lists OpenAI/Anthropic/Gemini-3 IDs **none of which we run** (we are Vertex Gemini 2.x) → in CRUD this is **stored and echoed verbatim**, never interpreted (mapping is a runtime concern).
- **`ConversationFlowNode`** = `oneOf` of **15** variants (`ConversationNode`, `SubagentNode`, `EndNode`, `FunctionNode`, `CodeNode`, `TransferCallNode`, `PressDigitNode`, `BranchNode`, `SmsNode`, `ExtractDynamicVariablesNode`, `AgentSwapNode`, `MCPNode`, `ComponentNode`, `BridgeTransferNode`, `CancelTransferNode`); each requires `type` (literal enum) + `NodeBase` (`id` required). Edges are typed (conditional / always / else / skip-response / success-failed); transitions are prompt-based or equation-based. **6a treats the node array as opaque JSON** — no per-node validation (that is the runtime's job).
- **SDK round-trip:** `retell.types:ConversationFlowResponse` (confirmed present in 5.53.0). Note the SDK aliases the wire keys `model_choice`/`model_temperature` to Python `api_model_choice`/`api_model_temperature` (Stainless workaround); **wire keys are unchanged**, so this only matters if a test inspects SDK model attributes — `model_validate(payload)` round-trips the wire shape fine.

---

## 5. Architecture & Persistence

A conversation flow is a **standalone entity referenced by agents**, not an agent — so it gets its **own table + service + router**, mirroring the Phase-5 Knowledge-Base build (own tables + `kb_service` + `routers/knowledge_bases`), **not** the retell-llm `agent_profiles` overlay. This keeps 6a purely additive and structurally immune to the Phase-3 "shared-table discriminator leak" class (the new table is referenced by zero existing readers).

### 5.1 New table `conversation_flows` (migration 0048, FORCE-RLS)
Plain per-org table, **no cross-org accessor** → **FORCE RLS** (the 0046 pattern, *not* the 0047 ENABLE-only KB exception). Columns:
- `id` — `Uuid` PK, server_default `gen_random_uuid()`.
- `organization_id` — `Uuid`, server_default `COALESCE(current_setting('app.current_org', true)::uuid, default_org_id())`, FK → `organizations.id`.
- `config` — `JSONB` NOT NULL — the persisted flow body (the create/merged-update payload, `exclude_none`).
- `version` — `Integer` NOT NULL default 0.
- `archived_at` — `DateTime(tz)` nullable (soft-delete; mirrors `delete-call`).
- `created_at` / `updated_at` — `DateTime(tz)` NOT NULL, server_default `now()` (`updated_at` `onupdate=now()`).
- Index `ix_conversation_flows_organization_id`.
- `_enable_rls('conversation_flows')` = ENABLE + **FORCE** + `CREATE POLICY tenant_isolation USING (organization_id = current_setting('app.current_org', true)::uuid) WITH CHECK (...)` + `GRANT SELECT, INSERT, UPDATE, DELETE … TO usan_app`.
- `down_revision = '0047'`; downgrade drops policy/index/table.

### 5.2 id-codec (`compat/ids.py`)
Add `_CONVERSATION_FLOW_PREFIX = "conversation_flow_"` + `encode_conversation_flow_id(uuid) -> "conversation_flow_" + uuid.hex` + `decode_conversation_flow_id(token) -> uuid` (delegates to the shared `_decode_hex`, which raises `CompatError(422)` on a missing prefix / bad hex). The oracle constrains `conversation_flow_id` only as `type: string` (no format), so the `<prefix><hex>` convention is conformant.

### 5.3 File structure (all new except the two noted edits)
- `apps/api/migrations/versions/0048_conversation_flows.py` *(new)*
- `apps/api/src/usan_api/db/models.py` — add `ConversationFlow` model (TenantScoped) *(edit, additive)*
- `apps/api/src/usan_api/compat/ids.py` — add the prefix + encode/decode *(edit, additive)*
- `apps/api/src/usan_api/repositories/conversation_flows.py` *(new)* — `create / get / update / archive / list` (keyset cursor), all `db.flush()`-only (caller commits).
- `apps/api/src/usan_api/compat/schemas/conversation_flow.py` *(new)* — request + response compat models.
- `apps/api/src/usan_api/compat/conversation_flow_service.py` *(new)* — the 5 handlers + `serialize_flow`.
- `apps/api/src/usan_api/compat/routers/conversation_flow.py` *(new)* — the 5 routes (`Depends(get_compat_db)`), mounted in the compat app alongside `knowledge_bases`.
- `apps/api/src/usan_api/compat/routers/unsupported.py` — remove the 5 **flow** entries from `_UNSUPPORTED` (keep the 5 component entries) *(edit)*.
- Tests (§9) + `docs/deployment/conversation-flows.md` *(new)*.

---

## 6. Endpoints & Semantics

**Request model** (`compat/schemas/conversation_flow.py`): a permissive Pydantic model (`extra="allow"`) so the full flow body is captured. Create-required fields typed loosely to enforce **presence** without over-constraining values:
- `start_speaker: str` (presence required on create),
- `model_choice: dict[str, Any]` (object; presence required on create),
- `nodes: list[Any]` (array; presence required on create).
All other oracle fields (`global_prompt`, `tools`, `components`, `mcps`, `knowledge_base_ids`, …) are accepted via `extra="allow"` and stored opaquely. **No** per-node-type / model-enum / edge validation in 6a (persisted-not-honored, forward-compatible — a future runtime adds semantic validation where it actually executes).

### 6.1 `POST /create-conversation-flow` → 201
1. `Depends(get_compat_db)` (Bearer→org, RLS).
2. Validate the 3 required fields present (Pydantic 422 first-field-only, no PHI) — a `create` request model with those three as required.
3. `config = body.model_dump(exclude_none=True)`; `repo.create(db, config=config, version=0)`; `db.commit()`; `db.refresh`.
4. Return `serialize_flow(row)` → 201.

### 6.2 `GET /get-conversation-flow/{conversation_flow_id}?version` → 200
1. `decode_conversation_flow_id` (422 on malformed).
2. `repo.get(db, id)` — RLS-scoped, excludes `archived_at IS NOT NULL` → 404 if missing/archived.
3. `?version` **accepted, ignored** (always current; documented).
4. Return `serialize_flow(row)`.

### 6.3 `PATCH /update-conversation-flow/{conversation_flow_id}?version` → 200
1. Decode + load (404 if missing/archived).
2. **Top-level shallow merge**: `new_config = {**row.config, **body.model_dump(exclude_none=True)}` — provided (non-None) top-level fields overwrite; omitted preserved (mirrors retell-llm's no-op-on-None PATCH). A provided `nodes`/`tools`/etc. **replaces** that whole top-level value (no deep node merge).
3. `version += 1`; `repo.update(db, id, config=new_config, version=...)`; `db.commit()`; `db.refresh`.
4. `?version` accepted, ignored.
5. Return `serialize_flow(row)`.

### 6.4 `DELETE /delete-conversation-flow/{conversation_flow_id}` → 204
1. Decode + load (404 if already archived/missing).
2. `repo.archive(db, id)` (set `archived_at`); `db.commit()`.
3. Return **204** (no body).

### 6.5 `GET /v2/list-conversation-flows` → 200 paginated
- **Self-contained keyset cursor** (the Phase-2 design, *not* the buggy calls.py `db.get` cursor): cursor = base64url(`created_at_iso|id_hex`); `repo.list` orders by `(created_at, id)` (respect `sort_order`, default desc), filters `archived_at IS NULL`, fetches `limit+1`, `has_more = len > limit`, `pagination_key` = encoded cursor of the last returned row (or null). `limit` default/clamp per the existing list endpoints (e.g. default 1000, max 1000 — match `list-retell-llms`/phone-numbers).
- Returns `{items: [serialize_flow(r) … ], has_more, pagination_key}`.

---

## 7. Validation, Errors, Serialization

### 7.1 Error envelope
RetellAI envelope `{"status":"error","message":"…"}`; status vocabulary per program §2.6. Validation → **422** (first-field-only, no PHI). Malformed id → 422 via `_decode_hex`. Not found / archived → **404**. Catch-all → 500 (type-name only). Never leak request bodies (flow `config` may carry prompts).

### 7.2 Serialization — `serialize_flow(row)`
`data = {**row.config}` (echo the stored body verbatim) then overlay the 3 server fields:
- `conversation_flow_id = encode_conversation_flow_id(row.id)`
- `version = row.version`
- `last_modification_timestamp = to_ms(row.updated_at or row.created_at)`

Return through a response model with `extra="allow"` and `.model_dump()`. Because we **echo only what was stored** (already `exclude_none` at persist time), unsent optional fields are naturally absent — satisfying the oracle's omit-nulls rule **without** an explicit `exclude_none` on the response (the same posture as `serialize_llm`).

### 7.3 Versioning
`version` starts at **0** on create, `+= 1` per update. `last_modification_timestamp` = `updated_at` ms (= `created_at` until first update — matches the oracle field description). `?version` on get/update is accepted and ignored (current-only; documented in §11).

### 7.4 The `kb_config` oracle quirk
`kb_config` carries a dual `type: object` + `$ref: KBConfig` annotation (technically invalid OAS 3.0). We **persist + echo** any `kb_config` blob opaquely (never interpret it). The frozen conformance fixtures **omit** `kb_config` (it is optional) so `assert_conforms` does not exercise the malformed sub-schema. (Recorded as a posture; revisit only if a runtime needs `kb_config`.)

---

## 8. RLS & Commit Discipline

- All 5 routes use `Depends(get_compat_db)` — Bearer `compat_api_keys` → org; the `after_begin` re-apply listener keeps `app.current_org` set across the post-commit `db.refresh` (the same mechanism retell-llm relies on).
- Repositories are `flush()`-only; the **service** owns the transaction boundary (`db.commit()` after each mutation) — the compat session does **not** autocommit.
- `organization_id` is **server-set** by the column default + RLS, never by app code.
- FORCE RLS means even the table owner is policy-bound on Cloud SQL; on CI the `usan` superuser bypasses RLS, so **RLS-meaningful assertions run on the non-superuser `app_session`** (the recurring program lesson). Isolation tests assert that org A cannot get/list/update/delete org B's flow.

---

## 9. Testing Strategy

- **`tests/compat/test_freeze_conversation_flows.py`** (`pytestmark = pytest.mark.frozen`): create→201, get, update, list-element each `assert_conforms(body, "ConversationFlowResponse")` + `assert_sdk_roundtrip(body, "retell.types:ConversationFlowResponse")`. Fixtures use a **valid** minimal flow (`start_speaker`, `model_choice={"type":"cascading","model":"gpt-4.1"}`, one valid `nodes` entry) and **omit `kb_config`** (§7.4).
- **`tests/compat/test_conversation_flow_crud.py`** (behavior, on `app_session` / `compat_client`): create persists version 0; get returns it; update merges top-level + bumps version + omitted fields preserved; `?version` ignored→current; delete→204 then get→404; list paginates (cursor `has_more`/`pagination_key`, archived excluded, `-n auto` sibling-tolerant by scoping asserts to created ids); malformed id→422; missing→404; **cross-org isolation** on `app_session` (org B cannot see org A's flow).
- **Surface coverage:** remove the 5 flow entries from `_UNSUPPORTED` (`unsupported.py`); `test_surface_coverage.py` / `KNOWN_GAPS` unchanged (served routes still appear in `_served()`). Edit **`tests/test_compat_fidelity.py`**: drop `('post','/create-conversation-flow')` from the 501 parametrize (~line 116) and swap the hardcoded "unsupported-still-requires-key" example (~line 142) to a still-stubbed path (e.g. a component op or a test-suite stub). The 5 **component** stubs stay 501 and remain covered.
- **mypy/ruff** clean (`uv run mypy` `files=["src"]`; ruff py314 line-100); `python -m py_compile` to verify any `except (A, B):` (display-artifact lesson).

---

## 10. Global Constraints (carried verbatim to the plan)

- **apps/api only** — `services/agent` untouched; no cross-service import.
- **Single new alembic migration 0048**; single head after merge; owner-DDL (runs as the `usan` owner per the deploy migration path).
- Ships **INERT** — no behavior change until a `v*` tag; no flag needed (the compat surface is key-gated and the flow is never executed in 6a).
- **`KNOWN_GAPS` stays `frozenset()`**; both surface-coverage files consistent; no new served op beyond the 5 (components remain 501).
- **`exclude_none` fidelity** preserved (omit-nulls): persist `exclude_none`, echo only stored fields.
- **CI mypy = `uv run mypy`** with config `files=["src"]` — never `mypy .`.
- **ruff** line-length 100, target py314 (apps/api).
- This env's text display strips parens from `except (A, B):` → verify syntax via `python -m py_compile`/ast, not by eye.
- **pytest `-n auto`** (parallel): tests tolerate sibling rows; **RLS-meaningful asserts run on the non-superuser `app_session`** (CI `usan` superuser bypasses RLS).
- Compat session does **not** autocommit — service commits explicitly after each mutation.
- Commit format `type(scope): description`, scope `api`/`docs`. Attribution disabled (no `Co-Authored-By`, no footer).
- **SDD:** never dispatch parallel implementers; post-review fixes go to ONE fix subagent with the full findings list.
- Squash-merge to protected `main` ONLY on explicit go-ahead; **no `v*` tag**.

---

## 11. Posture & Documented Deviations (6a)

- **Persisted-not-honored:** a created flow is stored + echoed conformantly but **never executed** at call/chat time (no runtime in 6a). Documented in `docs/deployment/conversation-flows.md`.
- **Accept-and-echo node graph:** no per-node-type / edge / model-enum semantic validation; only the 3 create-required top-level fields are presence-checked. A malformed graph is stored and echoed as-is (RetellAI would reject; we are lenient/forward-compatible — same posture class as phone-number bindings).
- **Current-only versioning:** `version` increments on update; `?version` on get/update is accepted but always serves current (no version history).
- **`model_choice` not mapped:** stored/echoed verbatim; the `LLMModel` enum values (which we don't run) are not validated or mapped (runtime concern).
- **`kb_config` quirk:** echoed opaquely; conformance fixtures omit it (§7.4).
- **No `name`:** flows have no name field (oracle); none is invented.

## 12. Open Follow-ups (later sub-phases — NOT 6a)

- **6b — Component CRUD:** the 5 `conversation-flow-component` ops; own `conversation_flow_components` table + `conversation_flow_component_` prefix; the delete→"creates local copies in all linked conversation flows" side-effect (or a documented-not-honored posture); `linked_conversation_flow_ids` echo.
- **6c — Agent ↔ flow binding fidelity:** validate `response_engine.type="conversation-flow"` + `conversation_flow_id` (same-org → 422 cross-org, never acknowledged) and **fix `serialize_agent`'s hard-coded `response_engine={type:"retell-llm",…}`** so the bound type/id echoes back cleanly (touches the frozen agent serializer — isolated from 6a on purpose).
- **6-runtime:** the greenfield DAG engine. Blast-radius map (this spec's research): the **chat path has a single chokepoint** (`compat/chat_service.generate_agent_reply`) → ~1 modification point; the **voice path has 4 `build_*_agent` sites + per-turn state + a new advance endpoint + a `call_flow_state` table** → 6+ points. Therefore the runtime is **chat-first, then voice**, and per-turn flow state must live in the API DB (worker is stateless / HTTP-only / no cross-import), read via a `/v1/tools/`-style endpoint that re-derives org from the call JWT (mirrors the Phase-5c `retrieve_kb_context` round-trip), SAVEPOINT-isolated so a state read never poisons the turn's write txn.
