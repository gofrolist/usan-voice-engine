# Phase 1 Data Model: RetellAI-Compatible Public API

This feature adds **one** persistent table (`compat_api_keys`) and otherwise presents
**compatibility *views*** over existing entities (`Call`, `Contact`, `CallBatch`/`CallBatchTarget`,
`AgentProfile`/`AgentProfileVersion`, `WebhookEndpoint`, the voice catalog). The "data model" here is
therefore mostly **mapping rules** between the RetellAI external contract and the native schema.

---

## 1. New entity — `compat_api_keys` (global / non-RLS)

The only new table. Global like `admin_users`/`memberships`/`invitations` because key lookup must
happen **before** the RLS org context exists.

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID PK | `gen_random_uuid()` |
| `organization_id` | UUID FK → `organizations.id` | the tenant this key authenticates as |
| `key_prefix` | text, indexed | first 8 chars of the token, plaintext, for O(1) candidate lookup + display |
| `key_hash` | text | sha256-hex of the **full** token (high-entropy random token; no KDF needed) |
| `status` | text | `active` \| `revoked` |
| `label` | text, nullable | human label (e.g. "crm-prod") |
| `created_at` | timestamptz | server default `now()` |
| `revoked_at` | timestamptz, nullable | set on revoke |
| `last_used_at` | timestamptz, nullable | best-effort async touch on each auth |

**Indexes**: `(key_prefix)` for lookup; `(organization_id)` for listing.
**Token shape**: `key_` + `secrets.token_urlsafe(32)`. Returned **once** at create (never re-readable),
mirroring the webhook-signing-secret pattern.
**Migration**: `apps/api/migrations/versions/0036_compat_api_keys.py` (next after `0035_invitations`).
**RLS**: none on this table (control-plane); rows are filtered by app code on `organization_id`. The
session it *opens* is RLS-scoped to `organization_id` via `set_tenant_context`.

**State transitions**: `active` → `revoked` (one-way; revoked keys are kept for audit, never reused).

---

## 2. Identifier codec (`compat/ids.py`) — reversible, no storage

| External (RetellAI) id | Native source | Encode | Decode |
|------------------------|---------------|--------|--------|
| `call_id` (bare 32-char) | `Call.id` (UUID) | `uuid.hex` | `UUID(hex=token)` |
| `agent_id` (`agent_…`) | `AgentProfile.id` (UUID) | `"agent_" + uuid.hex` | strip prefix → `UUID(hex=…)` |
| `llm_id` (`llm_…`) | `AgentProfile.id` (UUID) | `"llm_" + uuid.hex` | strip prefix → `UUID(hex=…)` |
| `batch_call_id` (`batch_call_…`) | `CallBatch.id` (UUID) | `"batch_call_" + uuid.hex` | strip prefix → `UUID(hex=…)` |
| `voice_id` (`retell-…`) | `cartesia_voice_id` (catalog) | alias map lookup | reverse alias map |

`agent_id` and `llm_id` are two prefixed views of the **same** `AgentProfile` row (one profile == one
response engine, see §5). Decode validates the prefix and rejects malformed ids with a documented
422.

---

## 3. Call (compat view) — assembled by `compat/call_serializer.py`

The RetellAI Call object is assembled from `Call` + transcript segments + recording + metrics +
analysis rows. Field mapping (RetellAI ← native):

| RetellAI field | Native source | Notes |
|----------------|---------------|-------|
| `call_id` | `Call.id` | codec (§2) |
| `call_type` | constant `"phone_call"` | web calls out of scope |
| `agent_id` | `Call.profile_override` or resolved profile | codec; `agent_<hex>` |
| `agent_version` | resolved `AgentProfileVersion.version` | int |
| `call_status` | `Call.status` | mapped (§4) |
| `from_number` / `to_number` | caller id / `Contact.phone_e164` | E.164 |
| `direction` | `Call.direction` | `inbound`/`outbound` |
| `telephony_identifier` | `{twilio_call_sid: Call.sip_call_id}` | LiveKit/Telnyx ref slot |
| `metadata` | `Call.dynamic_vars`-adjacent / CRM `metadata` | echoed |
| `retell_llm_dynamic_variables` | `Call.dynamic_vars` | passthrough |
| `start_timestamp` / `end_timestamp` | `Call.answered_at` / `Call.ended_at` | **ms** via `to_ms()` |
| `duration_ms` | `Call.duration_seconds × 1000` | ms |
| `transcript` / `transcript_object` | transcript segments | `transcript_object` = utterances w/ role+words |
| `recording_url` | `Call.recording_uri` (GCS, presigned) | PHI — allow-listed dests only |
| `disconnection_reason` | `Call.status` / `Call.end_reason` | mapped (§4) |
| `call_analysis` | summarization/analysis rows | `{call_summary, in_voicemail, user_sentiment, call_successful, custom_analysis_data}` |
| `call_cost` | metrics rows | `{combined_cost, product_costs[], …}` (best-effort; optional) |
| `latency` | metrics rows | optional |

Fields with no native source are emitted as `null` (RetellAI marks them `Optional`).

---

## 4. Call-status & disconnection-reason mapping (`compat/status_map.py`)

| Native `CallStatus` | Retell `call_status` | Retell `disconnection_reason` |
|---------------------|----------------------|-------------------------------|
| `queued`, `dialing` | `registered` | — |
| `ringing` | `registered` | — |
| `in_progress` | `ongoing` | — |
| `completed` | `ended` | `user_hangup` / `agent_hangup` |
| `voicemail_left` | `ended` | `voicemail_reached` |
| `no_answer` | `ended` | `dial_no_answer` |
| `busy` | `ended` | `dial_busy` |
| `failed` | `error` | `dial_failed` / `error_unknown` |
| `cancelled` | `ended` | `manual_stopped` |
| `dnc_blocked` | *(never on the wire — explicit create error, §6)* | — |

---

## 5. Agent + Response Engine (compat view) — bridged to AgentProfile/Version

**One `AgentProfile` == one agent == one response engine.** `agent_id` and `llm_id` both encode the
same `AgentProfile.id`.

| RetellAI agent field | Native target (`AgentProfile` / `AgentConfig`) | Notes |
|----------------------|-----------------------------------------------|-------|
| `agent_id` | `AgentProfile.id` | codec |
| `agent_name` | `AgentProfile.name` | |
| `response_engine.llm_id` | same `AgentProfile.id` | facade |
| `voice_id` | `AgentConfig.tts_voice_id` | via voice alias map (§7) |
| `language`, `voice_model`, `voice_temperature`, `voice_speed` | `compat_extras` blob | accepted/echoed; mostly no-ops |
| `webhook_url`, `webhook_events` | compat webhook subscription (§8) | drive compat deliveries |
| `version` | `AgentProfileVersion.version` (int) | |
| `base_version` | parent version | nullable |
| `is_published` | derived from `AgentProfile.published_version` | |
| `assigned_tags` | `compat_extras` / version note | echoed |
| `last_modification_timestamp` | `AgentProfile.updated_at` | **ms** |
| 100+ other agent fields | `compat_extras` blob inside `draft_config` | accept + echo, no-op |

| RetellAI Retell-LLM field | Native target | Notes |
|---------------------------|---------------|-------|
| `llm_id` | `AgentProfile.id` | codec `llm_<hex>` |
| `general_prompt` | `AgentConfig.system_prompt` | |
| `begin_message` | `AgentConfig` begin message slot | |
| `model`, `model_temperature`, `s2s_model` | **ignored** → Vertex pipeline | PHI containment (Constitution II) |
| `general_tools`, `states`, `starting_state` | mapped to `AgentConfig.tools` where representable; else `compat_extras` | |
| `knowledge_base_ids`, `mcps`, `kb_config` | accepted/echoed only | out of scope |

**Publish/version semantics**: reuse `agent_profiles_repo` draft/publish/version-history +
`draft_revision` optimistic concurrency. `create-agent`/`update-agent` write `draft_config`; publish
maps to `AgentProfile.published_version` + a new `AgentProfileVersion` row.

---

## 6. Contact resolution (number-first upsert)

RetellAI has no Contact resource; the compat layer lazily upserts on call/batch create.

| Field | Source on upsert |
|-------|------------------|
| `phone_e164` | `to_e164(to_number)` — the upsert key |
| `external_id` | CRM-supplied via `metadata.external_id` (exact key pinned against the oracle) — secondary correlation key |
| `name` | CRM-supplied via `metadata.name`, else the E.164 number (required by the model) |
| `timezone` | new `COMPAT_DEFAULT_TIMEZONE` setting (required by the model; drives quiet-hours) |
| `organization_id` | from the API key's org (RLS context) |

Lookup order: `get_contact_by_phone(phone_e164)` within the org; create when absent. Per-org
uniqueness on `phone_e164` and `external_id` already exists (migration `0034`).

**Synthesized idempotency_key** (when the CRM omits one): deterministic
`sha256(org | to_number | from_number | override_agent_id | retell_llm_dynamic_variables)`, namespaced
**outside** the reserved `sched:` / `batch:` prefixes, reusing the native `UNIQUE(idempotency_key,
org)` replay path.

---

## 7. Voice (compat view) — alias map over the catalog

| RetellAI Voice field | Native source (`VOICE_CATALOG`) |
|----------------------|----------------------------------|
| `voice_id` | reverse alias of `cartesia_voice_id` (e.g. `retell-Cimo`) |
| `voice_name` | catalog display name |
| `provider` | constant (`cartesia`) or catalog-declared |
| `accent`, `gender`, `age` | catalog metadata (nullable) |
| `preview_audio_url` | existing voice-sample endpoint URL |

An unmapped/unhosted `voice_id` on agent create/update returns a documented 4xx (FR-033), never an
opaque validation error.

---

## 8. Webhook subscription & delivery (compat view)

RetellAI configures webhooks on the **agent** (`webhook_url` + `webhook_events`). The compat layer
represents this as a compat-flavoured subscription tied to the agent, reusing the native
`WebhookEndpoint` + `WebhookOutbox` delivery machinery but with:

| Aspect | Native | Compat |
|--------|--------|--------|
| Event names | `call.started`, `call.completed`, … | `call_started`, `call_ended`, `call_analyzed` |
| Payload | `{event, occurred_at, data}` PHI-stripped | `{event, call: <full Call object>}` |
| Signature header | `X-Usan-Signature` (per-endpoint secret, sorted body) | `x-retell-signature` (API key, raw body + ts) |
| Destination guard | SSRF guard | SSRF guard **+** `COMPAT_WEBHOOK_ALLOWED_HOSTS` allow-list |
| Delivery infra | outbox + poller + breaker | **reused as-is** |
| Dedupe id | `delivery_id` | `delivery_id` (stable, injected pre-sign) |

---

## 9. Concurrency (compat view) — read-only synthesis

`get-concurrency` synthesizes the RetellAI concurrency object from settings + live in-flight count:

| RetellAI field | Source |
|----------------|--------|
| `current_concurrency` | live in-flight (non-terminal) call count for the org |
| `concurrency_limit` | `MAX_CONCURRENT_CALLS` |
| `base_concurrency` | `MAX_CONCURRENT_CALLS` |
| `reserved_inbound_concurrency` | `RESERVED_CONCURRENCY` |
| `purchased_concurrency`, `concurrency_purchase_limit`, `remaining_purchase_limit` | static `0` (single-VM engine) |
| `concurrency_burst_enabled` / `concurrency_burst_limit` | static (`false` / limit) |

---

## 10. New / changed settings

| Setting | Purpose |
|---------|---------|
| `COMPAT_DOCS_ENABLED` (bool, default false) | toggle the compat sub-app OpenAPI/docs independently of `DOCS_ENABLED` |
| `COMPAT_WEBHOOK_ALLOWED_HOSTS` (list) | attested in-infra destinations allowed to receive full-fidelity (PHI) webhooks |
| `COMPAT_DEFAULT_TIMEZONE` (str, IANA) | timezone for lazily-created contacts (drives quiet-hours) |
| compat rate-limit bucket | the CRM key's dedicated/elevated bucket (FR-054) |

All validated via Pydantic `BaseSettings` at startup (Constitution III).
