# Endpoint Contracts — RetellAI-Compatible Public API

All endpoints require `Authorization: Bearer <compat_api_key>`. See [README.md](./README.md) for the
shared conventions (versioning, ids, ms timestamps, `{status,message}` error envelope). Field names
are normative.

---

## Calls

### POST `/v2/create-phone-call` → 201
Request:

| Field | Type | Req | Bridge behavior |
|-------|------|-----|-----------------|
| `from_number` | string (E.164) | ✓ | outbound caller id |
| `to_number` | string (E.164) | ✓ | lazy-upsert Contact by phone (+`external_id` if in `metadata`) |
| `override_agent_id` | string | | resolved to an `AgentProfile` |
| `override_agent_version` | int \| string | | version/tag |
| `metadata` | object | | echoed on Call |
| `retell_llm_dynamic_variables` | object<string,string> | | → `Call.dynamic_vars` |
| `custom_sip_headers` | object<string,string> | | passthrough |

Response: the full **Call object** (see below), `call_status` typically `registered`.
**Gating**: if `to_number` is DNC-listed or outside quiet hours → **400** `{status:400, message:"blocked_dnc"|"blocked_quiet_hours"}`, no call placed (FR-015/SC-006).
**Idempotency**: a deterministic key is synthesized when omitted; retries return the existing call, never double-dial (FR-012).

### GET `/v2/get-call/{call_id}` → 200
Returns the full **Call object**.

### POST `/v3/list-calls` → 200
Request: `filter_criteria` (object — `agent_id`, `call_id`, `batch_call_id`, `call_status`,
`from_number`/`to_number`, `direction`, `start_timestamp`/`end_timestamp` ranges, …), `sort_order`
(`ascending`|`descending`, default `descending`), `limit` (default 50, max 1000), `pagination_key`
(opaque cursor) **xor** `skip` (int), `include_total` (bool).
Response: `{ items: Call[], pagination_key: string, has_more: bool, total?: int }`.

### POST `/v2/stop-call/{call_id}` → 204
Terminates an ongoing call. No body. Maps to a native in-flight cancel/end.

### PATCH `/v2/update-call/{call_id}` → 200
Request: `metadata`, `data_storage_setting`, `custom_attributes` (all optional). Maps onto
`Call.metadata`/`dynamic_vars`. Returns the updated Call object.

### Call object (shared by get-call / list-calls / webhooks)
`call_id`, `call_type` (`"phone_call"`), `agent_id`, `agent_name`, `agent_version`, `call_status`
(`registered`|`not_connected`|`ongoing`|`ended`|`error`), `from_number`, `to_number`, `direction`,
`telephony_identifier` (`{twilio_call_sid}`), `metadata`, `retell_llm_dynamic_variables`,
`collected_dynamic_variables`, `start_timestamp` (ms), `end_timestamp` (ms), `duration_ms`,
`transcript` (string), `transcript_object` (utterances: `{content, role, words[]}`),
`transcript_with_tool_calls`, `recording_url`, `public_log_url`, `disconnection_reason`,
`call_analysis` (`{call_summary, in_voicemail, user_sentiment, call_successful, custom_analysis_data}`),
`call_cost`, `latency`, `llm_token_usage`. Unset fields are `null`. See
[data-model.md](../data-model.md#3-call-compat-view-assembled-by-compatcall_serializerpy) for native
sourcing and [the status map](../data-model.md#4-call-status--disconnection-reason-mapping-compatstatus_mappy).

---

## Webhooks {#webhooks}

The agent's `webhook_url` receives `POST` with body `{ "event": <name>, "call": <Call object> }`:

| Event | When | Adds to the Call object |
|-------|------|-------------------------|
| `call_started` | call answered/ongoing | identity fields, `call_status:"ongoing"`, `start_timestamp` |
| `call_ended` | terminal | `end_timestamp`, `duration_ms`, `disconnection_reason`, `transcript*`, `recording_url`, `call_cost`, `latency`, `call_status:"ended"` |
| `call_analyzed` | analysis ready | `call_analysis{…}`, `collected_dynamic_variables` |

**Signature** header `x-retell-signature: v={ts_ms},d={hexdigest}` where the digest is the
lowercase-hex `HMAC_SHA256` of `raw_body + str(ts_ms)`, keyed by the `webhook_secret` (no
separator). The CRM verifies with the unmodified `retell-sdk verify(raw_body, webhook_secret,
signature)` — passing the **dedicated per-subscription `webhook_secret`** (returned ONCE at
registration), NOT its API key. (Decision US2: a dedicated secret — like the native webhook
secret / Stripe `whsec_` — so no recoverable copy of the bearer API key is ever stored; the
CRM sets its verify() key to this secret, a one-line migration step.) `ts_ms` is fresh
wall-clock ms at send time (the SDK enforces a ±5-min freshness window). Sign the exact bytes
sent (no re-serialization). The body stays byte-faithful `{event, call}`; the stable dedupe
`delivery_id` rides in the **`x-retell-delivery-id` header** (resolves the PENDING-FREEZE
header-vs-body question). Delivery: 2xx expected, 10s timeout, retry ladder (1m/5m/30m, ≤4
attempts) + per-subscription circuit breaker. **PHI**: full payloads only to
`COMPAT_WEBHOOK_ALLOWED_HOSTS`; an EMPTY allow-list rejects registration (no PHI ever leaves)
(FR-022/SC-005).

---

## Agents

### POST `/create-agent` → 201
Request: `response_engine` (`{type:"retell-llm", llm_id}`) ✓, `voice_id` ✓, `agent_name`, `language`,
`webhook_url`, `webhook_events` (subset of `call_started`/`call_ended`/`call_analyzed`),
`version_title`, + 100+ optional config fields (accepted, echoed, mostly no-ops).
Response (`AgentResponse`): `agent_id`, `version` (int), `base_version`, `assigned_tags`,
`is_published`, `last_modification_timestamp` (ms), plus the echoed config. Bridges to a new
`AgentProfile` (see [data-model.md §5](../data-model.md#5-agent--response-engine-compat-view-bridged-to-agentprofileversion)).

### GET `/get-agent/{agent_id}` (optional `?version=`) → 200 · `AgentResponse`
### GET `/list-agents` → 200 · **bare** `AgentResponse[]` (single inventory: admin-UI + API agents)
> Returns a bare array (do **not** wrap in `{items,…}`); accepts cursor query params
> `pagination_key` (last `agent_id`), `pagination_key_version`, `limit` (1–1000, default 1000).
### PATCH `/update-agent/{agent_id}` (optional `?version=`) → 200 · `AgentResponse` (PATCH semantics; `webhook_url`/`webhook_events` are patchable, FR-023)
### DELETE `/delete-agent/{agent_id}` → 204

> **Implemented semantics (US3)**: one `AgentProfile` IS the agent AND its response engine —
> `agent_id` (`agent_<hex>`) and `llm_id` (`llm_<hex>`) encode the same row. `create-retell-llm`
> creates that profile as an unpublished draft; `create-agent` (carrying `response_engine.llm_id`)
> binds the agent half onto it and **publishes** (the agent is live immediately — `is_published:true`,
> `version≥1`). Every subsequent `update-*` / `publish-agent-version` re-publishes (new version).
> `voice_id` accepts our `retell-<Name>` aliases or a raw curated cartesia id; `model` /
> `model_temperature` are echoed but **not honored** (Vertex pipeline, Constitution II).
> `delete-agent` **archives** (the agent disappears from `get`/`list`; config retained for audit) —
> not a hard delete. When `webhook_url` is set, the dedicated signing secret is returned **once** as a
> `webhook_secret` extra field on that create/update response (never on `get`/`list`; US2 decision).
> A duplicate `agent_name` is auto-deduped; a true concurrent collision returns **409**.

### POST `/publish-agent-version/{agent_id}` → 200 (FR-032 / US3 AS-3)
Publish a specific agent version. Body `{ version: int (≥0), version_title?, version_description? }`;
response = the published `AgentResponse`. Bridges to `agent_profiles.publish` / version-history.
Companion version endpoints in scope: `POST /create-agent-version/{agent_id}`,
`GET /get-agent-versions/{agent_id}`.
> **Pin against the oracle**: exact name (`publish-agent-version` vs `publish-agent`) and whether the
> CRM uses the dedicated publish call vs PATCH `update-agent` is confirmed against the captured usage;
> the publish capability itself is in scope (FR-032). The native draft/publish model supports either.

`voice_id` not hosted here → documented **4xx** (FR-033), not an opaque validation error.

---

## Retell-LLM (response engine)

### POST `/create-retell-llm` → 201
Request: `start_speaker` (`user`|`agent`) ✓, `general_prompt`, `begin_message`, `general_tools`,
`states`/`starting_state`, `model` (**ignored → Vertex pipeline**, PHI containment),
`model_temperature`, `knowledge_base_ids`/`mcps` (accepted/echoed only).
Response: `llm_id` (referenced by `response_engine.llm_id`), `version`, `is_published`,
`last_modification_timestamp` (ms), echoed fields. `llm_id` encodes the same `AgentProfile` as its
`agent_id`.

### GET `/get-retell-llm/{llm_id}` → 200 · ### PATCH `/update-retell-llm/{llm_id}` → 200 · ### DELETE `/delete-retell-llm/{llm_id}` → 204

### GET `/list-retell-llms` → 200 (FR-031)
List response engines. Returns `LlmResponse[]` (or a `{items, pagination_key, has_more}` cursor form).
> **Pin against the oracle**: the exact version prefix (root vs `/v2/list-retell-llms`) and the
> bare-array-vs-cursor response shape are confirmed against the captured CRM usage / live OpenAPI —
> the two research sources disagree on the prefix. The endpoint itself is in scope (FR-031).

---

## Batch

### POST `/create-batch-call` → 201
> **Unversioned path** — RetellAI serves create-batch-call at the root `/create-batch-call` (NOT
> `/v2/...`); a CRM repointing only its base URL must reach it here.

Request: `from_number` ✓, `tasks[]` ✓ (each: `to_number` ✓, `retell_llm_dynamic_variables`,
`override_agent_id`, `metadata`), `name`, `trigger_timestamp` (ms; omit = immediate),
`reserved_concurrency`, `call_time_window`.
Response: `batch_call_id`, `name`, `from_number`, `scheduled_timestamp` *(Unix **seconds** — the one
deliberate, RetellAI-faithful exception to the ms rule; the request `trigger_timestamp` and all Call
timestamps stay ms — assert this in a batch contract test)*, `total_task_count`, `call_time_window`.
Each task lazy-upserts a Contact and is gated per-target (DNC/quiet-hours). Bridges to
`batches_repo.create_batch_with_targets`.

**Implemented semantics (US4):**
- **Unversioned path** `/create-batch-call` (no `/v2`) → 201. `batch_call_id` is `batch_call_<hex>`.
- The batch is persisted `status='scheduled'`; the existing schedule poller materializes and **gates each
  target per-target** (DNC / quiet-hours / window / daily-cap) at dial time. No create-time DNC reject — a
  blocked number is skipped per-target, not a batch-level 4xx (faithful to RetellAI).
- **All-or-nothing validation**: every task is checked (E.164, reserved `__meta*` var keys, in-batch duplicate
  `to_number`, per-task `override_agent_id` liveness) before **any** Contact is upserted; any failure → `422`
  whose message names the offending `task[i]`, and nothing is persisted.
- `trigger_timestamp` (request) is epoch **ms**; `scheduled_timestamp` (response) is epoch **seconds** — the one
  deliberate exception. `reserved_concurrency` → native `max_concurrency`. `override_agent_id` is **per-task**
  (never batch-level). Per-task `metadata` is packed under the reserved `__meta__` dynamic-var key (full type
  fidelity), reusing US1's pack/upsert shims.
- `from_number` and `call_time_window` are **echoed, not honored** (the engine dials from its configured trunk;
  quiet-hours still enforced per-target by the poller). PENDING-FREEZE: the exact `call_time_window` shape + any
  mapping onto the native dial window.
- **Idempotency (Constitution V)**: RetellAI exposes none, so a deterministic key
  (`compat-batch:<sha256(org, from, trigger_ms, concurrency, tasks)>`, label-free) is synthesized; an identical
  resubmit **replays the same batch** (no second batch, no double-dial). A concurrent-insert race on the unique
  `(idempotency_key, organization_id)` resolves to the same replay, never a 500.

---

## Catalog (read-only)

### GET `/list-voices` → 200 · ### GET `/get-voice/{voice_id}` → 200
Voice object: `voice_id`, `voice_name`, `provider`, `accent`, `gender`, `age`, `preview_audio_url`.
Mapped from the curated catalog (RetellAI `voice_id` ⇄ `cartesia_voice_id`).

### GET `/get-concurrency` → 200
`current_concurrency`, `concurrency_limit`, `base_concurrency`, `purchased_concurrency`,
`concurrency_purchase_limit`, `remaining_purchase_limit`, `reserved_inbound_concurrency`,
`concurrency_burst_enabled`, `concurrency_burst_limit`. Synthesized from settings + live in-flight
count (see [data-model.md §9](../data-model.md#9-concurrency-compat-view--read-only-synthesis)).

**Implemented semantics (US5):**
- `list-voices` is a **bare array**; **deprecated** catalog voices are hidden from this new-selection
  list, but `get-voice` still resolves them (a live config may reference one). `provider` is `"cartesia"`;
  `accent`/`age`/`preview_audio_url` are `null` (the curated catalog does not track them — PENDING-FREEZE).
  `gender` maps `feminine→female` / `masculine→male` (else `null`). `voice_id` is the `retell-<Name>` alias.
- `get-voice/{voice_id}` accepts a `retell-<Name>` alias **or** a raw curated cartesia id (passthrough); an
  unhosted/unknown id → **404** `{status,message}` (a *hosted* voice is 200).
- `get-concurrency`: `current_concurrency` = the org's live in-flight count (`calls.count_in_flight`, RLS-scoped);
  `concurrency_limit` = `base_concurrency` = `concurrency_burst_limit` = `MAX_CONCURRENT_CALLS`;
  `reserved_inbound_concurrency` = `RESERVED_CONCURRENCY`; `purchased*`/`remaining_purchase_limit` = `0`;
  `concurrency_burst_enabled` = `false` (single-VM engine sells no extra concurrency).
- **Out-of-scope → `501`**: every endpoint in the list below is registered (and appears in the compat OpenAPI)
  returning `{status:501, message:"not_supported: <endpoint>"}`. The app-level auth gate still runs first, so an
  unsupported endpoint with no key returns `401`, not `501`.

---

## Open contract items — pin against the captured CRM oracle

These do not block the plan; each is fixed to RetellAI's exact shape from the captured CRM usage /
live OpenAPI before the contract is frozen:

- **Contact correlation keys**: the exact location for the CRM's `external_id` / `name` on
  create-phone-call (nested `metadata.external_id` / `metadata.name` vs top-level) — pin and add an
  example request to the Call contract + [data-model §6](../data-model.md#6-contact-resolution-number-first-upsert).
- **`delivery_id` transmission**: whether the webhook dedupe id is an HTTP header
  (e.g. `x-retell-delivery-id`) or a JSON body field (US2 AS-5).
- **`override_agent_version` string form**: the accepted string values (numeric string vs a reserved
  tag like `published`/`latest`) and the exact resolution rule onto `AgentProfileVersion.version`.
- **`custom_sip_headers`**: forwarding target (LiveKit/Telnyx SIP), restricted/reserved header names,
  and validation rules.

## Out-of-scope (stub → `501 not_supported`)
`/create-conversation-flow*`, `/create-knowledge-base*`, `/create-chat*`/`/create-chat-agent*`,
`/v2/create-web-call`, `/add-voice`/`/clone-voice`/`/search-voice`, `/create-batch-test*`/
`/*-test-case*`/`/*-test-run*`, `/create-phone-number`/`/import-phone-number`/`/list-phone-numbers`/…,
`/get-mcp-tools`, `/list-export-requests`, `/agent-playground-completion`. Each returns
`{status:501, message:"not_supported: <endpoint>"}` (FR-053/SC-009).
