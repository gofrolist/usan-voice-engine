<!--
SYNC IMPACT REPORT
==================
Version change: (template / unversioned) → 1.0.0
Added sections:
  - I. Service Isolation (new)
  - II. PHI Containment (new)
  - III. Type Safety & Validated Contracts (new)
  - IV. Test-First Development (new)
  - V. Idempotent Outbound Operations (new)
  - VI. Observability (new)
  - VII. Simplicity & YAGNI (new)
  - Security & Compliance Requirements (new)
  - Development Workflow (new)
  - Governance (filled from template placeholder)
Removed sections: none (all were template placeholders)
Templates requiring updates:
  - .specify/templates/plan-template.md ✅ Constitution Check gate already present
  - .specify/templates/spec-template.md ✅ no constitution references; no update needed
  - .specify/templates/tasks-template.md ✅ no constitution references; no update needed
Deferred items: none — all placeholders resolved
-->

# USAN Voice Engine Constitution

## Core Principles

### I. Service Isolation

`apps/api` and `services/agent` MUST NOT import from each other. All cross-unit
communication MUST occur via HTTPS (tool endpoints, webhook callbacks). No shared
modules, no in-process calls across service boundaries.

**Rationale**: Service isolation enables independent deployment, testing, and scaling.
It also prevents a failure or breaking change in one unit from cascading into the other
during live calls.

### II. PHI Containment (NON-NEGOTIABLE)

All Protected Health Information (elder demographics, wellness/medication logs, call
transcripts) MUST remain on HIPAA BAA-covered infrastructure. No PHI MUST egress to a
non-covered third-party API or service. All production LLM calls MUST use Vertex AI
(ADC-authenticated), not the Gemini Developer API. All API endpoints that access PHI
MUST emit structured audit log entries.

**Rationale**: USAN Retirement calls handle elders' medication status, mood, pain levels,
and contact information — all PHI. The project targets HIPAA readiness ahead of formal
certification; violations here create legal and reputational risk.

### III. Type Safety & Validated Contracts

All configuration MUST be validated via Pydantic `BaseSettings` at startup. All API
request/response bodies MUST use Pydantic models. Type hints MUST be present on every
function signature. `mypy` and `ruff` MUST pass in CI before any merge.

**Rationale**: Type errors in a live call pipeline (e.g., wrong `elder_id` type, missing
medication field) cause silent failures mid-conversation. Validated contracts catch these
at startup or in CI, never at runtime.

### IV. Test-First Development (NON-NEGOTIABLE)

Tests MUST be written before implementation (RED), confirmed failing, then implementation
written to make them pass (GREEN). Minimum 80% test coverage is required. No feature is
complete until its tests pass and coverage holds. Red-Green-Refactor cycle is strictly
enforced.

**Rationale**: Voice pipeline bugs surface as degraded elder UX — awkward pauses, missed
medications, wrong data. TDD forces explicit contract design before code, reducing
post-deploy regressions.

### V. Idempotent Outbound Operations

All outbound call dispatches MUST carry and honor an `idempotency_key`. Retry logic MUST
check the DNC list and quiet-hour gating before every attempt. A duplicate
`POST /v1/calls` with the same key MUST return the existing call record without re-dialing.

**Rationale**: Retry storms or duplicate webhook deliveries must never result in an elder
receiving the same call twice. DNC compliance is a regulatory requirement.

### VI. Observability

All API requests and agent lifecycle events MUST be logged as structured JSON via loguru
with `{name}` lazy placeholders. Errors MUST propagate — no silent swallowing. Cloud
Logging ingests journald driver output from `api` and `agent` containers. Log queries
MUST use `:` (contains) filters on `jsonPayload.message`, not `=` field equality.

**Rationale**: Live-call debugging requires a full audit trail. Silent failures are
invisible in production and block post-incident analysis.

### VII. Simplicity & YAGNI

The system MUST run on a single GCP VM via Docker Compose. No microservices split, no
Kubernetes, no additional services beyond what the design spec requires. Complexity MUST
be justified in the plan's Complexity Tracking table before introduction.

**Rationale**: The replacement target (RetailAI) handles 5,000–50,000 calls/month at
flat infra cost. Premature complexity raises operational cost without adding user value.

## Security & Compliance Requirements

- All secrets MUST be injected via environment variables loaded from GCP Secret Manager
  at boot. No secrets MUST be committed to source control.
- JWT tokens for agent ↔ API tool calls MUST be short-lived, per-call, and signed with
  `JWT_SIGNING_KEY` from the environment.
- All endpoints handling PHI MUST require authentication (JWT or service-to-service token).
- SSRF protections MUST guard any user-supplied URL or webhook destination.
- Inbound webhook payloads MUST have their signatures verified before processing
  (Telnyx HMAC, LiveKit webhook signatures).
- Rate limiting MUST be applied to all public-facing REST endpoints.

## Development Workflow

- Code MUST pass `ruff check`, `ruff format`, and `mypy` locally before push; CI enforces
  this gate on every PR.
- Every feature MUST produce a plan, spec, and task list under `.specify/memory/` before
  implementation begins.
- Database migrations MUST use Alembic and be reviewed for backward compatibility before
  merging.
- Docker images MUST use multi-stage builds, non-root UID 1001 `appuser`, and BuildKit
  cache mounts.
- The Makefile MUST remain the sole entry point for local stack management (`make up`,
  `make down`, `make logs`, `make base`).
- Production deploys MUST go through the `v*` tag + GitHub Actions pipeline; ad-hoc
  `docker compose up` on the VM is prohibited.
- New secret keys MUST be added to the VM's `.env` (via IAP-SSH or VM reboot) BEFORE
  cutting the deploy tag.

## Governance

This constitution supersedes all other project practices documented elsewhere. When a
conflict exists between a feature plan/spec and this constitution, the constitution governs.

**Amendment process:**
1. Open a PR with the proposed change to `.specify/memory/constitution.md`.
2. Increment the version per semantic versioning rules (Version Policy below).
3. Update any templates and plans that reference the amended principle.
4. PR description MUST include the rationale and any migration steps for existing code.

**Version Policy:**
- MAJOR: Principle removal, redefinition that changes non-negotiable rules, or governance
  overhaul.
- MINOR: New principle or section added; material expansion of existing guidance.
- PATCH: Clarification, wording fix, typo, or non-semantic refinement.

**Compliance review:** Principles MUST be checked at plan creation (Constitution Check
gate in `plan-template.md`) and re-checked after Phase 1 design.

**Version**: 1.0.0 | **Ratified**: 2026-06-13 | **Last Amended**: 2026-06-13
