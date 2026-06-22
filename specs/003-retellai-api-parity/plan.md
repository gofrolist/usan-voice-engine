# Implementation Plan: RetellAI-Compatible Public API

**Branch**: `003-retellai-api-parity` | **Date**: 2026-06-20 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/003-retellai-api-parity/spec.md`

## Summary

Expose a **RetellAI-compatible REST surface** on `apps/api` so the existing CRM migrates by
repointing its base URL + API key with no integration-code changes. The surface is delivered as
a **mounted FastAPI sub-application** (`compat_app`) that owns the RetellAI paths (`/create-agent`,
`/v2/*`, `/v3/*`, …), keeping the native `/v1` plane byte-for-byte unchanged. It authenticates a
static Bearer API key against a new org-scoped `compat_api_keys` table, sets the existing Postgres
RLS tenant context, and then **reuses the native call/contact/DNC/batch/agent-profile/webhook
services** — translating identifiers, timestamps (→ ms), status values, and the error envelope at
the edge. Webhooks are emitted in RetellAI's `{event, call}` shape with an `x-retell-signature` the
CRM's `retell-sdk verify()` accepts, full-fidelity to an allow-list of in-infrastructure
destinations. Number-first call creation lazily upserts a Contact; DNC/quiet-hours blocks return an
**explicit error** (stakeholder decision); the CRM-facing create returns **201** over the native
**202** enqueue.

## Technical Context

**Language/Version**: Python 3.14 (`apps/api`, managed with `uv`); ruff line-length 100, target py314.

**Primary Dependencies**: FastAPI + Starlette (sub-app mount), Pydantic v2, SQLAlchemy async +
asyncpg, Alembic, loguru, httpx (outbound webhook delivery), the existing LiveKit dispatch + dialer
seams. **No new third-party dependency** is introduced.

**Storage**: PostgreSQL (Cloud SQL in prod, `pgvector:pg18` in dev), Row-Level-Security enforced as
the non-superuser `usan_app` role. One new **global (non-RLS) table** `compat_api_keys`; no other
schema change (the compat layer reads/writes existing RLS-scoped tables via the tenant context).

**Testing**: pytest with testcontainers `pg18` and the RLS-subject `usan_app` connection harness,
FastAPI `TestClient`, the `two_orgs` isolation fixtures; `ruff check/format` + `uv run mypy` are the
pre-push gate (CI runs mypy on top of ruff — see [[ci_runs_mypy]]).

**Target Platform**: Linux server, single GCP VM via Docker Compose (Constitution VII).

**Project Type**: Web service — an **additive** RetellAI-compatible sub-surface inside the existing
`apps/api` FastAPI service. No frontend, no new service (Constitution I keeps `apps/api` and
`services/agent` isolated; the compat layer lives entirely in `apps/api`).

**Performance Goals**: Carry the CRM's migrated call volume within the existing single-VM
concurrency cap (`MAX_CONCURRENT_CALLS`); auth on the hot path is O(1) via a `key_prefix` index +
constant-time hash compare; webhook deliveries dispatched within ~10s of the underlying event
(SC-003), reusing the proven outbox poller.

**Constraints**: Zero native `/v1` regressions (SC-007); PHI never egresses to a non-allow-listed
destination (SC-005, Constitution II); all compat timestamps in **milliseconds** (FR-051); RetellAI
`{status, message}` error envelope on the compat surface only; the CRM key gets a dedicated/elevated
rate-limit bucket (FR-054).

**Scale/Scope**: Single-org runtime today, multi-org-capable through RLS; replacement target volume
~5k–50k calls/month. In scope: calls (create/get/list/stop/update), call webhooks
(`call_started`/`call_ended`/`call_analyzed`), agent + Retell-LLM config, batch calling, voices &
concurrency reads. Out of scope: conversation-flow, knowledge-base, chat, web-call, voice
clone/add/search, eval suite, phone-number management, MCP/export/playground/custom-LLM websocket.

## Constitution Check

*GATE: must pass before Phase 0 research. Re-checked after Phase 1 design — still PASS.*

| Principle | Status | How the plan complies |
|-----------|--------|-----------------------|
| **I. Service Isolation** | ✅ PASS | The entire compat layer lives under `apps/api/src/usan_api/compat/` and imports only `apps/api` repos/services. It never imports from `services/agent`; outbound dialing flows through the existing `livekit_dispatch` HTTPS seam exactly like native `/v1/calls`. |
| **II. PHI Containment** | ✅ PASS | RetellAI `model` (e.g. `gpt-4.1`) is ignored and routed to the Vertex-backed pipeline — no conversation PHI to a non-BAA LLM. Full-fidelity webhooks (transcript/recording/analysis) deliver **only** to `COMPAT_WEBHOOK_ALLOWED_HOSTS`, layered atop the existing two-stage SSRF guard. Every PHI-touching compat endpoint requires the API key and emits a structured audit line binding `organization_id` + key id (never the token, never PHI). |
| **III. Type Safety & Validated Contracts** | ✅ PASS | Every compat request/response is a Pydantic v2 model under `compat/schemas/`; the id codec, signer, and serializers are fully type-annotated; `ruff` + `mypy` gate locally and in CI. |
| **IV. Test-First Development** | ✅ PASS | Contract, integration, RLS-isolation, signature-replica, and mount-isolation tests are written **first** (RED) against the existing pg-backed harness; target ≥80% coverage. The captured CRM-usage inventory (spec Dependencies) is the acceptance oracle. |
| **V. Idempotent Outbound Operations** | ✅ PASS | A deterministic `idempotency_key` is synthesized when the CRM omits one (namespaced outside the reserved `sched:`/`batch:` prefixes), reusing the native `UNIQUE(idempotency_key, org)` replay path so retries never double-dial. DNC + quiet-hours gating is always enforced and returns an **explicit error** (never bypassed, never a silent drop). |
| **VI. Observability** | ✅ PASS | All compat logs use loguru lazy placeholders binding ids/org/outcome only. A catch-all compat exception handler logs the exception type-name and returns `{status:500, message:"internal error"}` so a traceback never leaks PHI nor escapes as the native `{detail}` shape. |
| **VII. Simplicity & YAGNI** | ⚠️ JUSTIFY | One new global table, one mounted sub-app, a deterministic id **codec** instead of a persistent alias table; webhook delivery, batch materialization, agent versioning, and SSRF guarding are **reused**. The intentional second surface (sub-app + a duplicate compat webhook signer/serializer) is justified in Complexity Tracking. |

**Initial gate: PASS** (one justified complexity, recorded below). **Post-Phase-1 re-check: PASS** —
the design adds no new service, no new external dependency, and exactly one table + one sub-app.

## Project Structure

### Documentation (this feature)

```text
specs/003-retellai-api-parity/
├── plan.md              # This file
├── research.md          # Phase 0 — technical decisions (signature, schemas, internals, FastAPI)
├── data-model.md        # Phase 1 — entities, id codec, status/timestamp mapping, the new table
├── quickstart.md        # Phase 1 — runnable validation scenarios
├── contracts/           # Phase 1 — RetellAI-compatible API contracts
│   ├── README.md        #   conventions: base URL, auth, error envelope, id formats, timestamps, out-of-scope stubs
│   ├── endpoints.md     #   all in-scope endpoints (calls, agents, retell-llm, batch, catalog) + webhook envelope/signature
│   └── admin-compat-keys.md  # native /v1/admin key issuance/list/revoke
├── checklists/
│   └── requirements.md  # spec quality checklist (from /speckit-specify)
└── tasks.md             # Phase 2 — created by /speckit-tasks (NOT this command)
```

### Source Code (repository root)

The compat surface is an additive subpackage in `apps/api`; native `/v1` code is unchanged except
three small, additive wiring edits (mount, admin-key router, settings/rate-limit keys).

```text
apps/api/
├── src/usan_api/
│   ├── compat/                      # NEW — the RetellAI-compatible surface (isolated)
│   │   ├── app.py                   #   builds & configures the mounted compat sub-app
│   │   ├── auth.py                  #   get_compat_db: Bearer key → org-scoped RLS session
│   │   ├── errors.py                #   CompatError + {status,message} exception handlers
│   │   ├── ids.py                   #   reversible per-resource id codec (call_id, agent_/llm_/batch_call_)
│   │   ├── voice_map.py             #   Retell voice_id ⇄ Cartesia voice_id alias map
│   │   ├── serialization.py         #   to_ms()/duration_ms() + shared converters (FR-051)
│   │   ├── webhook_signature.py     #   Retell symmetric signer (x-retell-signature)
│   │   ├── call_serializer.py       #   build the full RetellAI Call object from native rows
│   │   ├── call_create.py           #   call-create service: number→Contact upsert + DNC/quiet-hours gating + idempotency synthesis
│   │   ├── agent_bridge.py          #   agent + Retell-LLM ⇄ AgentProfile/AgentProfileVersion
│   │   ├── webhook_delivery.py      #   {event,call} envelope, allow-list gate, reuse outbox+signer
│   │   ├── status_map.py            #   CallStatus ⇄ Retell call_status + disconnection_reason
│   │   ├── schemas/                 #   Pydantic request/response models (calls, agents, retell_llm, batch, voices)
│   │   └── routers/                 #   calls (/v2,/v3), agents (+publish-version), retell_llm (+list), batches (unversioned /create-batch-call), catalog, unsupported
│   ├── repositories/compat_api_keys.py   # NEW — lookup_by_prefix/create/list/revoke/touch_last_used
│   ├── routers/admin_compat_keys.py      # NEW — /v1/admin super-admin key issuance (native plane)
│   ├── schemas/compat_api_keys.py        # NEW — admin key issue/list/revoke models (token returned once)
│   ├── main.py                      # CHANGED — mount compat_app after native routers; register admin router; startup path-collision assertion
│   ├── settings.py                  # CHANGED — COMPAT_DOCS_ENABLED, COMPAT_WEBHOOK_ALLOWED_HOSTS, COMPAT_DEFAULT_TIMEZONE, compat rate-limit bucket
│   └── ratelimit.py                 # CHANGED — compat-aware bucket for the CRM key (FR-054)
├── migrations/versions/0036_compat_api_keys.py   # NEW — global compat_api_keys table
└── tests/
    ├── test_compat_auth.py          # key valid/invalid/revoked → 401; org-scoping sets RLS
    ├── test_compat_calls.py         # create→get→list→stop→update; number→Contact upsert; idempotency; DNC/quiet-hours explicit error
    ├── test_compat_webhook_signature.py   # retell-sdk verify() replica accepts our signer; rejects tampered/wrong-key/stale
    ├── test_compat_agents.py        # agent + retell-llm bridge round-trip; versioning/publish; voice aliasing; single inventory
    ├── test_compat_batches.py       # create-batch-call per-task upsert + gating + Retell-shaped response
    ├── test_compat_mount_isolation.py     # /health + /v1 still native after mount; collision assertion; {status,message} vs {detail}; not-supported stubs
    └── test_compat_rls_isolation.py # org-A key never sees org-B calls/agents/batches
```

**Structure Decision**: Single additive subpackage `apps/api/src/usan_api/compat/` plus one repo,
one admin router, one schema module, and one migration. The RetellAI surface is served by a **mounted
sub-application** (chosen in Phase 0) so its error envelope, OpenAPI/docs, and auth baseline are
isolated from the native `/v1` app with zero native edits. Many small files (Constitution-aligned
file organization), each <400 lines.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|--------------------------------------|
| **Mounted compat sub-app** (a second FastAPI surface beside `/v1`) | Starlette does **not** share exception handlers, OpenAPI schema, or app-level dependencies across a mount — a sub-app is the only way to emit the RetellAI `{status,message}` envelope + separate docs + a uniform API-key auth baseline while leaving the native `/v1` `{detail}` handlers byte-for-byte unchanged (FR-004/SC-007). | APIRouters-with-prefixes on the existing app force `request.url.path` branching inside every shared exception handler and intermix the native + compat OpenAPI — brittle, higher blast radius on the native surface. |
| **Duplicate compat webhook signer + Call serializer** (alongside the native `webhook_signing.py` / `webhook_events.py`) | The native signer is `X-Usan-Signature: v={ms},d=HMAC(secret, "{ms}."+canonical_sorted_body)` with a per-endpoint secret; RetellAI requires `x-retell-signature: v={ms},d=HMAC(api_key, raw_body+str(ms))`. The schemes are structurally incompatible, and the native event payloads are deliberately PHI-stripped so they cannot carry the full Call object the CRM needs. | Reusing the native signer/payload builders would make `retell-sdk verify()` reject every delivery and would omit the transcript/analysis the CRM consumes. The compat path still **reuses** the native outbox/retry/circuit-breaker/SSRF delivery machinery — only the signer, serializer, envelope, and the destination allow-list differ. |
