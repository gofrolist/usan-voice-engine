# RetellAI Parity Phase 6b — Conversation-Flow Components CRUD (design)

**Date:** 2026-06-30 · **Type:** Phase design spec · **Status:** Approved — ready for implementation plan

> Phase 6a (conversation-flow CRUD) merged as #150 (squash `f488555`). Phase 6b promotes the five
> already-501-stubbed `conversation-flow-component` endpoints to LIVE, as a structural port of the
> frozen 6a pattern. Persisted-not-honored: the component body is stored as opaque JSONB and echoed
> conformantly, but not executed at call/chat time (the DAG runtime is a later sub-phase).

## 1. Goal & scope

Promote these five endpoints from documented-501 to served, conformant against the pinned oracle
(`openapi-final.yaml`, retell-sdk 5.53.0):

| Method | Path | Success | Notes |
|---|---|---|---|
| POST | `/create-conversation-flow-component` | 201 | required: `name`, `nodes` |
| GET | `/get-conversation-flow-component/{conversation_flow_component_id}` | 200 | current-only |
| PATCH | `/update-conversation-flow-component/{conversation_flow_component_id}` | 200 | shallow merge, null-clears |
| DELETE | `/delete-conversation-flow-component/{conversation_flow_component_id}` | 204 | plain soft-delete |
| GET | `/v2/list-conversation-flow-components` | 200 | keyset pagination |

The five stubs currently live in `compat/routers/unsupported.py` (lines 27–31) and are removed here.

**Non-goals.** No runtime execution; no agent↔component or flow↔component linking (6c / 6-runtime).
No versioning (the oracle response has no version field). No real "local copies" delete fan-out
(see §7) — we cannot back it and do not fake it (capability-bounded parity).

## 2. Oracle ground truth

- **`ConversationFlowComponentResponse`** = `CreateConversationFlowComponentRequest` + two required
  server fields: `conversation_flow_component_id` (string) and `user_modified_timestamp`
  (int64, ms).
- **`CreateConversationFlowComponentRequest`** = `ConversationFlowComponent` + required `name`,
  `nodes`. All other fields (`flex_mode`, `tools`, `mcps`, `nodes[]`, `edges`, …) are optional and
  opaque to us.
- **Update** is `PATCH`, body `ConversationFlowComponent` (all fields optional).
- **Delete** description: *"When deleting a shared component, creates local copies for all linked
  conversation flows."* — a runtime behavior we cannot back yet (§7).

**Key divergence from 6a:** the component response carries `user_modified_timestamp` and **no
`version`** field. 6a's flow response carried `version` + `last_modification_timestamp`. Therefore
the 6b table has no `version` column and the serializer emits no version.

## 3. Data model — migration 0049

New table `conversation_flow_components`:

| Column | Type | Notes |
|---|---|---|
| `id` | uuid pk | `gen_random_uuid()` default |
| `org_id` | uuid | `TenantScoped` (FK organizations, per convention) |
| `config` | JSONB, not null | opaque component body (server keys stripped before store) |
| `archived_at` | timestamptz, null | soft-delete marker |
| `created_at` | timestamptz, not null | `now()` |
| `updated_at` | timestamptz, not null | `now()`, `onupdate=now()` → drives `user_modified_timestamp` |

**RLS: FORCE** (plain per-org, exactly like 6a's 0048 `conversation_flows`) — *not* the 0047 KB
ENABLE-only exception. There are no cross-org accessors, so FORCE binds the owner correctly and the
table is structurally leak-immune (zero external readers). Grant `SELECT, INSERT, UPDATE, DELETE`
to `usan_app`. A migration test asserts all four grants and `relforcerowsecurity = true`.

## 4. ID codec (`compat/ids.py`)

Add `_CONVERSATION_FLOW_COMPONENT_PREFIX = "conversation_flow_component_"`:

- `encode_conversation_flow_component_id(uuid) -> str` → prefix + hex
- `decode_conversation_flow_component_id(token) -> uuid.UUID` → `_decode_hex(..., kind="conversation_flow_component_id")` (malformed → `CompatError(422)`)
- `encode_/decode_conversation_flow_component_cursor(...)` → delegate to the shared
  `_encode_keyset_cursor` / `_decode_keyset_cursor` helpers (same DRY the 6a `/review` landed).

## 5. Schemas + serializer (`compat/schemas/conversation_flow_component.py`)

- `CreateConversationFlowComponentRequest`: `ConfigDict(extra="allow")`; presence-check only `name:
  str` and `nodes: list[Any]`. All other fields ride through unvalidated.
- `UpdateConversationFlowComponentRequest`: `ConfigDict(extra="allow")`, no declared fields
  (any subset of top-level fields accepted, shallow-merged by the router).
- `serialize_component(row) -> dict`:
  ```
  data = dict(row.config)
  data["conversation_flow_component_id"] = ids.encode_conversation_flow_component_id(row.id)
  data["user_modified_timestamp"] = int(row.updated_at.timestamp() * 1000)
  return data
  ```
  No `version`.

## 6. Repository (`repositories/conversation_flow_components.py`)

Direct port of `conversation_flows.py`, minus the `version` param:

- `create(db, *, config) -> ConversationFlowComponent`
- `get(db, component_id) -> ... | None` (filters `archived_at IS NULL`)
- `update(db, component_id, *, config) -> ... | None` (get → mutate `config` → flush/refresh)
- `archive(db, component_id) -> bool` (set `archived_at = now()`)
- `list_components(db, *, limit, descending, after) -> list[...]` — keyset over `(created_at, id)`,
  fetch `limit + 1` so the caller computes `has_more` without a COUNT. RLS scopes to the org.

## 7. Router (`compat/routers/conversation_flow_component.py`)

Port of 6a's `conversation_flow.py`, mounted in `compat/app.py`.

- `_SERVER_KEYS = ("conversation_flow_component_id", "user_modified_timestamp")` — stripped from the
  stored config on **both create and update** (defense-in-depth against a future reader of
  `row.config[...]`; `serialize_component` always derives them from ORM columns).
- **create** → strip server keys from provided (non-null) fields, persist, `commit`, 201.
- **get** → decode id, `get`, `None` → `CompatError(404)`.
- **update (PATCH null-clears)** → `get` (None → 404); shallow top-level merge over `row.config`:
  a sent non-null field overwrites, a sent explicit **null pops** the key (removed → omitted from
  echo, matching the oracle's omit-nulls), an omitted field is preserved; strip server keys;
  `update` (None → 404 — TOCTOU of a concurrently-archived row returns 404, not 500); `commit`.
- **delete** → `archive`; `False` → `CompatError(404)`; `commit`; 204. The oracle's "local copies
  for all linked conversation flows" fan-out is **not backed** — nothing links components to flows
  at rest (components ride inside flow `config` as opaque JSON), so we soft-delete and document the
  gap. Accept-and-echo discipline, same as 6a.
- **list** → lenient cursor decode (`contextlib.suppress(CompatError)` → first page), `list_components`,
  return `{"items": [...], "has_more": bool, "pagination_key"?: str}`.
- `_audit(request, op)` → PHI-free `compat_org_id` + `op` only, never the config.

Remove the five stub tuples from `unsupported.py`.

## 8. Tests

Mirror the 6a suite:

- `tests/compat/test_conversation_flow_component_crud.py` — create/get/update/delete/list happy
  paths + null-clear + server-field-overwrite (client-injected server keys ignored) + missing
  required field → 422 + update-of-archived → 404 + delete-twice → 404.
- `tests/test_conversation_flow_component_schemas.py` — required-field validation + opaque
  passthrough + serializer server fields.
- `tests/test_conversation_flow_components_repo.py` — repo CRUD + keyset pagination + RLS scoping.
- `tests/test_conversation_flow_components_migration.py` — four `usan_app` grants +
  `relforcerowsecurity = true`.
- `tests/compat/test_freeze_conversation_flow_components.py` — conformance freeze: responses
  validate against the pinned oracle `ConversationFlowComponentResponse` and decode through
  retell-sdk 5.53.0.
- `tests/test_compat_fidelity.py` — bump served/stub endpoint counts (5 stubs → served).

## 9. Operator note

`docs/deployment/conversation-flow-components.md` — records: persisted-not-honored; the
not-backed delete "local copies" fan-out; FORCE-RLS own table (leak-immune); inert until the next
`v*` tag deploy (merged ≠ deployed).

## 10. Foundational-principle compliance

- **§2.3 Documented-501 → served:** exact versioned paths; five stubs promoted, no path drift.
- **§2.4 Auth + RLS:** inherits the compat bearer/RLS plane; org-scoped table.
- **§2.6 Error envelope:** 404 not-found, 422 malformed id / missing required field, via `CompatError`.
- **§2.2 Capability-bounded:** no faked runtime linking; delete fan-out documented, not faked.
