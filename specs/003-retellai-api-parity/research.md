# Phase 0 Research: RetellAI-Compatible Public API

This document records the technical decisions that resolve the unknowns in the plan's Technical
Context. Findings come from four parallel research streams (RetellAI webhook signature, exact
RetellAI request/response schemas, the current `apps/api` internals, and FastAPI mounting/auth
patterns) plus a consolidation pass, with corrections from a deep read of the live code.

---

## Decision 1 — Mount the RetellAI surface as a sub-application

**Decision**: Add a dedicated mounted FastAPI sub-app `compat_app`, attached in `create_app()`
**after** all native `/v1` routers + `/health` via `app.mount("/", compat_app)`. The compat surface
is three routers on `compat_app`: an **unprefixed** router (`/create-agent`, `/list-agents`,
`/get-agent/{id}`, `/update-agent/{id}`, `/delete-agent/{id}`, `/publish-agent-version/{id}`,
`/create-retell-llm` + CRUD + `/list-retell-llms`, `/create-batch-call`, `/list-voices`,
`/get-voice/{id}`, `/get-concurrency`), a **`/v2`** router (`create-phone-call`, `get-call`,
`stop-call`, `update-call`), and a **`/v3`** router (`list-calls`). **Note**: `create-batch-call` is
RetellAI-unversioned (root path), so it lives on the unprefixed router, NOT `/v2`.
The static-key dependency is set at the `compat_app` level. A startup assertion iterates `app.routes`
and fails if any compat path string-equals a native path.

**Rationale**: A mounted sub-app gives — in isolation and with zero native edits (FR-004/SC-007) —
its own exception-handler table (Starlette does **not** propagate parent handlers into a mount, so
the native `{detail}` handlers stay untouched while compat emits `{status,message}`), its own
OpenAPI/docs gated by a separate flag, and its own app-level auth baseline. The RetellAI paths span
disjoint prefixes (`/create-agent` AND `/v2/*` AND `/v3/*`) that no single `include_router` prefix
can cover. `main.py` already attaches every router to `app`, so the mount is a one-line additive
seam. Native surface is verified to be entirely `/v1` + `/health` — disjoint from the root-level
RetellAI paths, so the greedy `/` mount is collision-free today; the startup assertion protects a
future native root path.

**Alternatives considered**: (a) APIRouters-with-prefixes on the existing app — rejected: forces
`request.url.path` branching inside every shared exception handler and intermixes native + compat
OpenAPI. (b) ASGI path-rewrite middleware (`/v2/*` → `/v1/*`) — rejected: most fragile, couples wire
compatibility to internal routing that will move, and does not actually save the field/id/timestamp
translation work nor solve the envelope/OpenAPI separation.

---

## Decision 2 — API-key auth plane bridged to the existing RLS tenant context

**Decision**: New **global (non-RLS)** table `compat_api_keys` (`organization_id` FK, `key_prefix`
indexed plaintext, `key_hash` = sha256-hex of the full token, `status` active|revoked, `label`,
`created_at`, `revoked_at`, `last_used_at`). Tokens are `key_` + `secrets.token_urlsafe(32)`,
returned **once** at create (mirroring `webhook_signing.generate_secret`). A new `get_compat_db`
dependency: parse the Bearer credential via `HTTPBearer(auto_error=False)`, look up active candidates
by `token[:8]` (the index seam — never a table scan), `hmac.compare_digest(sha256_hex(token),
row.key_hash)` in constant time (the `auth.require_operator_token` discipline), then open a session
and call `tenant_context.set_tenant_context(session, row.organization_id)` — re-applied on every
transaction via the **same `after_begin` listener** `get_tenant_db` already uses. Best-effort async
`touch_last_used`. Key issue/list/revoke (FR-003) is a new **native** `/v1/admin` super-admin router,
**not** on the compat surface.

**Rationale**: Key lookup must precede tenant context, so the table is global like
`admin_users`/`memberships`/`invitations` (the established control-plane precedent — see
[[multitenancy_roadmap]]). `set_tenant_context` issues `set_config('app.current_org', org,
is_local=true)` — the identical RLS seam `get_db`/`get_tenant_db` use — so every reused repository
(`calls_repo`, `contacts_repo`, `batches_repo`, `agent_profiles_repo`, `webhook_outbox`) is
automatically org-scoped with **zero** changes. In prod the app connects as the non-superuser
`usan_app` role, so RLS is already enforcing; this dependency only selects **which** org. Reusing the
`after_begin` re-apply listener prevents the post-commit default-org fallback bug that listener
exists to fix (see [[multitenancy_roadmap]] — the v0.6.1 worker-RLS regression).

**Alternatives considered**: A static `OPERATOR_API_KEY`-style env key (rejected: FR-003 needs
issue/list/revoke + per-org scoping; an env key can't be revoked without redeploy and carries no
org). bcrypt/argon2 hashing (rejected: 256-bit random tokens are not passwords, so a KDF work-factor
only slows the hot auth path; sha256 + prefix index + `compare_digest` is correct). RLS-protecting
`compat_api_keys` itself (rejected: lookup precedes context, needing a bootstrap exception; the
global-table precedent is cleaner).

---

## Decision 3 — Identifier translation: reversible edge codec, per-resource format

**Decision**: Edge translation, never re-minting native ids, in a pure `compat/ids.py`
(`encode`/`decode`, deterministic, no DB round-trip), with **per-resource formats that match
RetellAI's actual shapes**:

| Resource | RetellAI format | Compat representation |
|----------|-----------------|------------------------|
| `call_id` | 32-char alphanumeric, **no prefix** | the native Call UUID rendered as 32-char hex (`uuid.hex`) — decoded back via `UUID(hex=...)` |
| `agent_id` | opaque string | `agent_<uuid-hex>` over the AgentProfile UUID |
| `llm_id` | `llm_…` | `llm_<uuid-hex>` (the response-engine facade id) |
| `batch_call_id` | `batch_call_…` | `batch_call_<uuid-hex>` over the CallBatch UUID |
| `voice_id` | e.g. `retell-Cimo` | static alias map ⇄ the curated `cartesia_voice_id` (`compat/voice_map.py`) |

**Rationale**: The spec's Identifier-strategy assumption mandates least-invasive edge aliasing with a
stable reversible mapping rather than native re-minting. Native UUIDs already exist on
Call/AgentProfile/CallBatch; a deterministic codec is reversible with no storage, so
get/list/webhook all derive the same external id from the same row (FR-050). **Correction over the
initial synthesis**: RetellAI `call_id` is a bare 32-char token (e.g. `Jabr9TXYYJ…`), so the native
UUID hex (exactly 32 hex chars) is a closer match than a `call_`-prefixed value; only `agent_`,
`llm_`, and `batch_call_` carry prefixes, matching RetellAI. The voice catalog is already a code
constant with a membership set, so a code alias map matches the catalog-as-code precedent (see
[[voice_catalog_and_live_debug]]).

**Alternatives considered**: A persistent `id_alias` table (rejected as YAGNI — a deterministic codec
over the existing UUID is reversible without storage; revisit only if the id spaces must drift, per
the spec's stated trigger). Re-minting native ids (rejected: invasive, breaks the native
admin/runtime planes).

---

## Decision 4 — Replicate RetellAI's webhook signature exactly (verify()-compatible)

**Decision**: New `compat/webhook_signature.py` implementing RetellAI's **symmetric** scheme,
separate from the native `webhook_signing.py`:

- Header: `x-retell-signature: v={ts_ms},d={hexdigest}` where `ts_ms = int(time.time()*1000)`.
- `digest = HMAC_SHA256(key=api_key.encode("utf-8"), msg=(raw_body_str + str(ts_ms)).encode("utf-8")).hexdigest()` — **lowercase hex**.
- The HMAC key is the **same shared compat API key** the CRM passes to `verify()`.
- **Sign-what-you-send**: capture the exact body bytes once and reuse them byte-identically for both
  signing and the POST (never re-serialize between sign and send).

The four load-bearing pitfalls: **(1)** the timestamp is part of the signed message (`body + str(ts)`,
not the body alone); **(2)** milliseconds, not seconds; **(3)** raw body only; **(4)** lowercase hex.
A contract test runs a faithful replica of `retell-sdk`'s symmetric `verify()` (parse
`v=(\d+),d=(.*)`, enforce `abs(now_ms - ts) <= 300000`, recompute HMAC over `body + str(ts)`) and
asserts our header passes for in-window timestamps and fails on tampered body / wrong key / stale ts.

**Rationale**: The research empirically proved the scheme is a symmetric HMAC keyed solely by the
shared API key — no Retell-private secret — and that an independently computed
`v={ts},d=HMAC(api_key, body+str(ts))` is accepted by the real `verify()` logic. The native signer is
fundamentally different (`X-Usan-Signature`, message `f"{ts}."+canonical_sorted_body`, a per-endpoint
secret, key-sorted JSON), so it **cannot** be reused; a dedicated compat signer is required.

**Alternatives considered**: Reuse the native signer (rejected: different header, different
signed-message construction, and a per-endpoint secret instead of the API key — `verify()` rejects
it). RSA-PSS/base64 asymmetric path (rejected: only selected when the secret is a PEM key, never for
API-key webhooks). Re-serializing JSON after signing (rejected: breaks raw-body verification).

> **Note (SDK version caveat)**: the current npm `retell-sdk@5.x` removed the bundled `verify()`
> helper; the **Python** SDK still ships it. The **wire scheme is identical** across versions, so our
> signature is accepted by older TS SDKs and the Python SDK. If the CRM is on a newer TS SDK without
> `verify()`, it verifies with the documented byte recipe — which we match exactly.

---

## Decision 5 — Full-fidelity webhooks to an allow-listed destination, PHI-safe

**Decision**: Emit `call_started` / `call_ended` / `call_analyzed` in the RetellAI envelope
`{"event": …, "call": <full Call object>}` via a **new compat delivery path separate from** the
native `webhook_events`/`webhook_outbox` fan-out (which emits PHI-stripped `{event, occurred_at,
data}` with the `X-Usan` signature and the native event names `call.started`/`call.completed`/…).
The compat path: (1) `compat/call_serializer.py` assembles the full RetellAI Call object (ms
timestamps, `transcript_object`, `recording_url`, `call_analysis`, `disconnection_reason`,
`telephony_identifier`) from Call + transcript + recording + analysis rows; (2) the agent's RetellAI
`webhook_url` + `webhook_events` are registered as a compat-flavoured `WebhookEndpoint` variant
restricted to an **allow-list** of attested in-infra hosts (new `COMPAT_WEBHOOK_ALLOWED_HOSTS`)
checked **in addition** to the existing two-stage SSRF guard (`ssrf_guard.validate_webhook_url` at
registration + `resolve_public_or_raise` before every POST); (3) delivery **reuses** the existing
transactional-outbox + at-least-once + circuit-breaker + delivery-poller machinery, signing with the
compat (Retell) signer instead of the native one and injecting a stable delivery id for dedupe.

**Rationale**: Stakeholder decision — the CRM is inside covered infrastructure, so full-fidelity
payloads are an internal data flow, not third-party egress; the allow-list is defense against
misconfiguration (FR-022/SC-005). The native fan-out deliberately strips PHI and cannot carry
transcripts, so it must not be reused for the compat payload; the compat path reuses the proven
delivery infrastructure while substituting the envelope, serializer, signer, and host allow-list. The
10s-timeout/3-retry/dedupe contract matches RetellAI's documented webhook delivery contract and
SC-003.

**Alternatives considered**: Reuse the native PHI-stripped builders (rejected: cannot emit
transcript/recording/analysis; wrong envelope/signature). Deliver to any SSRF-passing host (rejected:
SSRF blocks only internal/unroutable hosts, not arbitrary public covered-vs-non-covered destinations
— the explicit allow-list is the PHI-containment control FR-022 requires). A brand-new delivery
worker (rejected: duplicates the battle-tested outbox/breaker/poller).

---

## Decision 6 — Call-create mapping (number→Contact, idempotency, DNC/quiet-hours explicit error, 201)

**Decision**: `POST /v2/create-phone-call` in the compat `/v2` router:

1. **Normalize + upsert Contact**: `to_e164(to_number)`, then `contacts_repo.get_contact_by_phone`;
   if absent, `contacts_repo.create_contact` with `name` defaulting to the E.164 number (or a
   CRM-supplied `metadata` name) and `timezone` defaulting to a new **`COMPAT_DEFAULT_TIMEZONE`**
   setting, preserving the CRM's `external_id` when supplied (FR-011).
2. **Synthesize idempotency_key** when omitted: a deterministic sha256 over (org, to_number,
   from_number, override_agent_id, retell_llm_dynamic_variables), namespaced **outside** the reserved
   `sched:`/`batch:` prefixes, so retries never double-dial (FR-012) and never collide with
   materializer-owned keys.
3. **Gate BEFORE dialing — explicit error**: check `dnc_repo.is_blocked` and an explicit **create-time
   quiet-hours** check (`quiet_hours.next_allowed` against the contact timezone); if blocked, raise
   `CompatError` → 4xx with a machine-readable reason (`blocked_dnc` / `blocked_quiet_hours`) and do
   **not** place the call (FR-015/SC-006).
4. **Dispatch via reused internals**: `dnc_repo.lock_phone` → `calls_repo.create_call` →
   `livekit_dispatch.dispatch_agent` → `dialer.schedule_dial`, then translate to a RetellAI Call
   object (`call_status` mapped per Decision 7) with HTTP **201**.

**Rationale**: RetellAI is number-first + immediate-dial + 201; the native engine is contact-first +
202-enqueue + idempotency + a DNC-row-with-200. The compat layer presents RetellAI's contract on the
wire while reusing the native dispatch internals for the actual dial. **Two corrections from the live
code**: (a) native `POST /v1/calls` returns **202** and, on DNC, creates a `DNC_BLOCKED` Call row and
returns **200** — the compat path must instead block *before* `create_call` and return an explicit
error, so it does **not** reuse the native auto-DNC-row behavior; (b) native quiet-hours gating only
runs at *retry* time, so the compat create handler adds an explicit create-time quiet-hours check.
Lazy Contact upsert needs a `name` and `timezone` that RetellAI's create-call payload does not carry —
hence the number-as-name default and `COMPAT_DEFAULT_TIMEZONE`.

**Alternatives considered**: Mirror native DNC (200 + `DNC_BLOCKED` row) — rejected: violates the
explicit-error decision (FR-015/SC-006). Require a pre-existing Contact — rejected: breaks number-first
drop-in (FR-011). Return the native 202 — rejected: RetellAI returns 201 with a populated Call object
and the CRM expects a `call_id` immediately.

---

## Decision 7 — Call-status + disconnection-reason mapping

**Decision**: A pure `compat/status_map.py` maps the native `CallStatus`
(`queued`/`dialing`/`ringing`/`in_progress`/`completed`/`voicemail_left`/`no_answer`/`busy`/`failed`/
`dnc_blocked`/`cancelled`) onto RetellAI's `call_status`
(`registered`/`not_connected`/`ongoing`/`ended`/`error`) and, for terminal calls, onto a RetellAI
`disconnection_reason`. Indicative mapping (pinned in data-model):

| Native CallStatus | Retell `call_status` | Retell `disconnection_reason` |
|-------------------|----------------------|-------------------------------|
| queued, dialing | registered | — |
| ringing | registered | — |
| in_progress | ongoing | — |
| completed | ended | user_hangup / agent_hangup |
| voicemail_left | ended | voicemail_reached |
| no_answer | ended | dial_no_answer |
| busy | ended | dial_busy |
| failed | error | dial_failed / error_unknown |
| cancelled | ended | manual_stopped |
| dnc_blocked | (not surfaced as a call — explicit create error per Decision 6) | — |

**Rationale**: The CRM reads `call_status`/`disconnection_reason` to reconcile outcomes; an explicit
mapping keeps the wire contract RetellAI-shaped while the engine keeps its richer internal enum.
`dnc_blocked` never reaches the wire because compat blocks before dialing (Decision 6).

---

## Decision 8 — Agent + Retell-LLM bridged onto AgentProfile / AgentProfileVersion

**Decision**: `compat/agent_bridge.py` bridges the RetellAI agent + response-engine resources onto
the existing `AgentProfile`/`AgentProfileVersion` model:

1. `create-agent` / `update-agent` map RetellAI fields (`response_engine.llm_id`, `voice_id`,
   `language`, `webhook_url`, `webhook_events`, `version_title`) onto `AgentProfile.draft_config`
   (the existing `AgentConfig`: `system_prompt`, `llm_model`, `stt_model`, `tts_voice_id`,
   `tts_provider`, `tools`, `custom_variables`); the 100+ unused RetellAI agent fields are accepted,
   stored in a `compat_extras` blob inside the config, and echoed for parity but are no-ops.
2. Versioning/publish maps onto the native draft/publish/version-history: RetellAI `version` (int) ==
   `AgentProfileVersion.version`; `is_published`/published state derives from
   `AgentProfile.published_version`; publish/create-version/list-versions reuse
   `agent_profiles_repo`; `last_modification_timestamp` = `updated_at` in ms.
3. `create-retell-llm` returns an `llm_id` facade (Decision 3) mapping
   `general_prompt`/`model`/`begin_message` into the profile draft; RetellAI `model` (e.g. `gpt-4.1`)
   is **ignored** and routed to the Vertex-backed pipeline (Constitution II — never a third-party LLM).
4. `voice_id` is translated through `compat/voice_map` against the voice catalog; an unhosted voice
   returns a documented error (FR-033).

Agents created via the admin UI **and** via the compat API share **one** `AgentProfile` inventory, so
`list-agents` shows both (FR-030 / AS-3.5).

**Rationale**: The spec mandates a single agent inventory across both planes, bridged onto the existing
draft-publish model rather than a parallel store. `AgentProfile` already has
`draft_config` + `published_version` + `AgentProfileVersion` history + `draft_revision`, which map
cleanly onto RetellAI's `agent_id`/`version`/`base_version`/`is_published`. Ignoring RetellAI `model`
satisfies PHI containment.

**Alternatives considered**: A separate compat agent store (rejected: breaks single-inventory and
duplicates versioning). Honoring RetellAI `model` literally (rejected: egresses PHI to a non-BAA LLM).
Storing all 100+ RetellAI fields as first-class columns (rejected: YAGNI; accept+echo in a blob).

---

## Decision 9 — Batch-call bridged onto `/v1/batches`

**Decision**: `POST /create-batch-call` (RetellAI-unversioned root path) bridges onto the existing batch model: each RetellAI
`task` (`to_number` + per-target `retell_llm_dynamic_variables` + overrides) is lazy-upserted to a
Contact (Decision 6's shim) and mapped to a `CreateBatchRequest` target; the batch is created via
`batches_repo.create_batch_with_targets`, reusing the all-or-nothing validation, payload-digest
replay, `trigger_at` scheduling, and per-target DNC/quiet-hours gating already in
`routers/batches.py` + the schedule orchestrator. The response is a RetellAI-shaped batch object
(`batch_call_id`, `name`, `from_number`, `scheduled_timestamp`, `total_task_count`,
`call_time_window`).

**Rationale**: FR-040/FR-041 — the engine already has a close behavioral equivalent (batch +
per-target dynamic variables + scheduling + gating). Reusing `create_batch_with_targets` + the
orchestrator means each target flows through the same gated/tracked/webhook path as US1; only
request/response translation + the per-task number→Contact upsert are new.

**Alternatives considered**: A new batch engine (rejected: duplicates proven machinery). Dialing batch
tasks immediately bypassing the orchestrator (rejected: loses scheduling, concurrency throttle, gating).

---

## Decision 10 — Timestamp (ms) + error-envelope translation

**Decision**: (1) All compat timestamps are Unix epoch **milliseconds** and durations are `*_ms`
(FR-051), via one `compat/serialization.py:to_ms(datetime)->int` seam used by every serializer (the
engine stores tz-aware datetimes + `duration_seconds`). (2) Three handlers registered **on
`compat_app` only**: a `CompatError(http_status, message)` handler →
`JSONResponse({"status": http_status, "message": message}, status_code=http_status)`; a
`StarletteHTTPException` override → `{"status": exc.status_code, "message": str(exc.detail)}`
(honoring `is_body_allowed_for_status_code` so 204 stays body-less); a `RequestValidationError`
override → `{"status": 422, "message": <flattened first error>}`. Plus a catch-all `Exception`
handler → `{"status": 500, "message": "internal error"}` so a traceback never leaks PHI nor escapes
as the native `{detail}` shape. Per-endpoint codes: **201** on create-phone-call/create-agent/
create-batch-call/create-retell-llm; **204** body-less on stop-call/delete-agent/delete-retell-llm;
**400** on the DNC/quiet-hours explicit block; **401** from `get_compat_db`; **422** for schema
validation. Out-of-scope endpoints (FR-053/SC-009) get explicit stub routes raising
`CompatError(documented "not_supported: <endpoint>")`, kept in the compat OpenAPI.

**Rationale**: Because handlers are scoped to the mounted `compat_app`, the native `/v1` `{detail}`
handlers are untouched (SC-007) — the structural payoff of the sub-app choice. RetellAI's envelope
repeats the numeric HTTP status in the body's `status` field, so building the body from the same code
set on the response is correct. One `to_ms` helper guarantees FR-051 consistency.

**Alternatives considered**: Branch on `request.url.path` inside shared handlers (rejected: only
needed without a sub-app; brittle). Emit seconds/mixed units (rejected: FR-051 mandates ms). Let
out-of-scope endpoints 404 silently (rejected: SC-009 requires a documented "not supported").

---

## Decision 11 — Testing approach (test-first, reuse the pg harness)

**Decision**: Test-first against the existing conftest harness (testcontainers `pg18`, `alembic
upgrade head`, the `usan_app` RLS-subject role, `two_orgs` fixtures, `TestClient`). New test modules:
(1) **contract** tests asserting each compat path/method/field-name/status/envelope matches the
captured RetellAI usage oracle; (2) a **webhook-signature** contract test running a `retell-sdk
verify()` replica against our signer (in-window pass; tampered/wrong-key/stale fail); (3)
**integration** tests for the full create→get→list→stop→update lifecycle, number→Contact upsert,
synthesized-idempotency no-double-dial, and the DNC/quiet-hours explicit error; (4) an **RLS
isolation** test proving an org-A key never sees org-B calls/agents/batches; (5) a **mount-isolation**
test asserting `/health` + a `/v1` route still hit native handlers after the mount, the startup
collision assertion fires on a shadow, and compat errors are `{status,message}` while `/v1` stays
`{detail}`; (6) agent/llm bridging round-trip + out-of-scope "not supported" stub tests. Target ≥80%
coverage; run `uv run pytest -v`, `ruff check/format`, and `uv run mypy` before push.

**Rationale**: The repo already has a mature pg-backed harness with RLS-subject connections and
per-org isolation fixtures, so the compat suite extends proven patterns. Test-first is mandated
(Constitution IV); the captured CRM-usage inventory is the explicit acceptance oracle. The signature
replica is the only way to guarantee byte-for-byte SDK `verify()` compatibility (SC-003). Mypy is part
of the local gate (CI runs it — see [[ci_runs_mypy]]).

---

## Resolved unknowns (non-blocking, pin at implementation against the captured oracle)

These do not block the plan; they are pinned against the captured CRM-usage inventory before fixing
the exact contract:

1. **Batch timestamp units (RESOLVED)** — `create-batch-call` request `trigger_timestamp` is **ms**;
   response `scheduled_timestamp` is **seconds** — a deliberate, RetellAI-faithful exception to the
   ms rule (FR-051). All other timestamps stay ms. A batch contract test asserts the difference.
2. **Agent publish surface (RESOLVED — now an explicit contract)** — a dedicated publish endpoint
   (`POST /publish-agent-version/{agent_id}` with `{version, version_title?, version_description?}`,
   plus `create-agent-version` / `get-agent-versions`), bridged to the `AgentProfile` draft/publish
   model (FR-032). The exact endpoint name (`publish-agent-version` vs `publish-agent`) and whether
   the CRM uses it vs PATCH `update-agent` is the only piece pinned against the captured oracle.
3. **`stop-call`/`delete-agent` 204-vs-200** — the spec lists 204; confirm per-endpoint from the
   oracle (a body-less 204 vs an enveloped 200 changes the handler return type).
4. **Compat rate-limit bucket** — confirm the desired throughput/bucket for the CRM key (FR-054); the
   mounted sub-app does not inherit the native rate-limit middleware, so it is re-applied with a
   compat-aware key.

---

## Key reuse map (from the current `apps/api` internals)

The compat layer calls these existing seams rather than reimplementing them:

| Concern | Reused symbol (file) |
|---------|----------------------|
| Create call | `repositories/calls.py:create_call` |
| Guarded status transitions + event enqueue | `repositories/calls.py:set_status`, `mark_answered` |
| Contact lookup / create | `repositories/contacts.py:get_contact_by_phone`, `create_contact` |
| DNC lock + check | `repositories/dnc.py:lock_phone`, `is_blocked` |
| Quiet-hours window | `quiet_hours.py:next_allowed` |
| Agent config resolution | `repositories/agent_profiles.py:resolve_agent_config`, `is_live_profile`, publish/list_versions |
| Outbound dispatch | `livekit_dispatch.py:dispatch_agent`, `dialer.py:schedule_dial` |
| Batch create | `repositories/batches.py:create_batch_with_targets` |
| Webhook enqueue + delivery infra | `repositories/webhook_outbox.py:enqueue_event`, `webhook_delivery.py` (poller/breaker), `ssrf_guard.py` |
| RLS tenant context | `tenant_context.py:set_tenant_context` (+ the `after_begin` re-apply listener used by `auth.get_tenant_db`) |
| Static-bearer constant-time compare | the `auth.require_operator_token` pattern |
