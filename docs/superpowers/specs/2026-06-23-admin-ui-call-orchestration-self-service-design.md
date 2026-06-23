# Admin-UI Call-Orchestration Self-Service — Design Spec

**Date:** 2026-06-23
**Status:** Draft for review
**Author:** Evgenii Vasilenko (with Claude Code)

## 1. Purpose & context

The RetellAI-parity work (feature 003 + the native `/v1` surface) expanded what an
organization can configure, but the **call-orchestration plane** — per-contact
recurring schedules, ad-hoc calls, the Do-Not-Call (DNC) list, and full contact
lifecycle — is today reachable **only via the `OPERATOR_API_KEY` machine plane**, not
through human org-admin RBAC in the admin UI. An org admin cannot, in the SSO admin-ui,
add a contact, schedule their morning/evening check-in, place a one-off call, or manage
DNC without backend/operator access.

This spec brings that plane under **org-admin self-service** in the admin-ui so an org
can run a daily-wellness program end-to-end, org-isolated, with zero operator access.

**Scope (the "full self-service slice"):** Contacts create/edit + Schedules CRUD +
Call-now + DNC management.

**Explicit non-goals (deferred to later phases):** one-off batch calling, outbound
webhook-endpoint configuration, org-admin self-service of RetellAI-compat API keys, and
the `admin_profile_tests` super-admin→org-admin authz relaxation. We also do **not**
surface RetellAI-only concepts with no native mapping (conversation-flow state machines,
knowledge bases, phone-number/trunk provisioning, voice cloning).

**Out of scope by design:** the admin-ui must **not** call the RetellAI-compat sub-app
endpoints. The compat plane is an external drop-in for third-party CRMs; the admin-ui
edits the authoritative **native** config and always will.

## 2. Key facts that shape the design

These were verified against the codebase (`apps/api/src/usan_api`) during brainstorming:

- **The call-plane tables are already tenant-scoped.** `Contact`, `CallSchedule`,
  `DNCEntry`, `Call`, `CallBatch`, `WebhookEndpoint` all inherit `TenantScoped`:
  `organization_id` is defaulted by a DB column default sourced from
  `COALESCE(current_setting('app.current_org', true)::uuid, default_org_id())` and
  enforced by RLS. So org isolation already exists at the row level.
- **Two auth planes exist side by side.** The orchestration routers
  (`routers/schedules.py`, `routers/dnc.py`, `POST /v1/calls`) are gated by
  `require_operator_token` and use the plain `get_db` session (machine plane, used by the
  scheduler poller, batch executor, and the compat bridge). The admin routers
  (`routers/admin_*.py`) are gated by `require_admin_session` /
  `require_admin_role(AdminRole.…)` and use `get_tenant_db`, which sets
  `app.current_org` from the authenticated SSO session's org → RLS isolation.
- **`POST /v1/calls` enforces DNC + profile-liveness, but NOT quiet-hours.** It locks the
  phone, replays on idempotency-key, 422s on a non-live `profile_override`, and returns a
  `DNC_BLOCKED` call (HTTP 200) when the number is on the DNC list. It does not consult
  the contact's dial window — ad-hoc calls intentionally bypass quiet-hours.
- **`origin` is derived from the idempotency-key prefix, and there is NO `adhoc:` prefix.**
  Only `sched:` and `batch:` are reserved; `parse_origin` returns `null` for everything
  else (including a `NULL` key). The admin calls-list `origin=adhoc` filter matches
  "`NULL` key OR non-reserved key". So the "Call now" wrapper mints a **plain unique
  non-reserved** idempotency key (e.g. `f"admin-{uuid4()}"`) — never a prefixed one; the
  resulting call reads `origin=null` and lands in the adhoc bucket.
- **The admin contacts router is partial.** `routers/admin_contacts.py` exposes only
  `GET` list, `PUT /{id}/profile`, and `PUT /{id}/timezone` — no create, detail, field
  edit, or delete.
- **The DNC router has no list read.** `routers/dnc.py` exposes only add (`POST`) and
  remove (`DELETE /{phone}`).

## 3. Architecture

### 3.1 Decision: new `/v1/admin/*` wrappers (not re-gating in place)

Re-gating the operator endpoints to accept SSO sessions would break the machine plane
(scheduler/batch/compat all carry the operator token and run on `get_db`), and the SPA
must never hold the shared operator key. Instead we **add new `/v1/admin/*` endpoints**:

- gated by `require_admin_session` for reads and `require_admin_role(AdminRole.ADMIN)`
  for writes (matches `admin_contacts` / `admin_calls` precedent),
- running on `get_tenant_db` so every query is RLS-isolated to the caller's org,
- each write emitting a **PHI-free** `admin_audit` row (ids + action + client IP only —
  never name/phone/vars), matching the existing routers.

The existing operator-token endpoints are left **untouched**.

### 3.2 Shared-service extraction (the one refactor)

The call/dispatch core and the schedule-window math currently live inline in the
operator routers on `get_db`. To avoid divergence, Phase 0 extracts session-agnostic
service helpers that both planes call with whatever session they hold:

- **`services/outbound_calls.py`** (new) — `enqueue_outbound_call(db, *, contact, ...)`
  factoring the contact-lookup → DNC lock/block → idempotency replay → liveness 422 →
  `_create_and_dispatch` flow out of `routers/calls.py`. The operator `POST /v1/calls`
  and the new admin `POST /v1/admin/calls` both delegate here; behavior (DNC, liveness,
  dispatch, retries) is identical.
- **Schedule-window validation** — the fail-closed `next_run_at` / 422 logic in
  `routers/schedules.py` (`_compute_next_run_at`) is moved/shared so the admin schedule
  wrappers enforce the exact same window/quiet-hours rules.

Repositories (`call_schedules`, `contacts`, `dnc`, `calls`) are already session-agnostic
and reused as-is; only a `dnc.list_entries` read is added (see §4.4).

### 3.3 Component boundaries

| Unit | Responsibility | Depends on |
|---|---|---|
| `services/outbound_calls.py` | enqueue+dispatch core, plane-agnostic | repos, `livekit_dispatch`, `dialer` |
| `routers/admin_contacts.py` (extended) | contact lifecycle for org admins | `contacts` repo, audit |
| `routers/admin_schedules.py` (new) | schedule CRUD for org admins | `call_schedules` repo, window validation, audit |
| `routers/admin_calls.py` (extended) | call-now enqueue for org admins | `outbound_calls` service, audit |
| `routers/admin_dnc.py` (new) | DNC list/add/remove for org admins | `dnc` repo, audit |
| admin-ui `features/contacts` (extended) | contact create/edit/detail UI | API client |
| admin-ui `features/schedules` (new) | per-contact + global schedule UI | API client |
| admin-ui `features/dnc` (new) | DNC list/add/remove UI | API client |
| admin-ui call-now action + modal | ad-hoc call trigger with ack | API client |

## 4. API surface (Phase 0)

All endpoints are `get_tenant_db` (RLS), audited PHI-free, and follow the response/error
conventions of the existing admin routers.

### 4.1 Contacts — extend `routers/admin_contacts.py`

| Endpoint | Verb | Role | Notes |
|---|---|---|---|
| `/v1/admin/contacts` | POST | ADMIN | create: `name`, `phone_e164`, `timezone`, `external_id?`, `preferred_voice?`, `metadata?` |
| `/v1/admin/contacts/{id}` | GET | VIEWER | detail (masked phone) |
| `/v1/admin/contacts/{id}` | PATCH | ADMIN | edit any of the create fields |
| `/v1/admin/contacts/{id}` | DELETE | ADMIN | delete a contact (subject to existing FK/in-use guards) |

Existing `PUT /{id}/profile` and `PUT /{id}/timezone` remain (no behavior change).
Validation reuses the existing E.164 (`E164_PATTERN`) and IANA-timezone validators.
Uniqueness is `(phone_e164, organization_id)` and `(external_id, organization_id)` →
a violation maps to HTTP 409.

### 4.2 Schedules — new `routers/admin_schedules.py`

| Endpoint | Verb | Role | Notes |
|---|---|---|---|
| `/v1/admin/schedules` | GET | VIEWER | list; filters `contact_id?`, `last_result=skipped_window?` |
| `/v1/admin/schedules` | POST | ADMIN | create: `contact_id`, `slot`, window, `days_mask`, `enabled`, `dynamic_vars?`, `profile_override?` |
| `/v1/admin/schedules/{id}` | GET | VIEWER | detail |
| `/v1/admin/schedules/{id}` | PATCH | ADMIN | edit; recomputes `next_run_at`, fail-closed 422 on bad window/tz |
| `/v1/admin/schedules/{id}` | DELETE | ADMIN | delete (also the PHI-removal path: DELETE or PATCH-clear `dynamic_vars`) |

One-schedule-per-slot and quiet-hours-intersection rules are enforced via the shared
window validation (§3.2). A non-live `profile_override` 422s (same contract as operator).

### 4.3 Call-now — extend `routers/admin_calls.py`

| Endpoint | Verb | Role | Notes |
|---|---|---|---|
| `/v1/admin/calls` | POST | ADMIN | enqueue ad-hoc outbound; body `contact_id`, `dynamic_vars?`, `profile_override?` |

- Delegates to `services/outbound_calls.enqueue_outbound_call`.
- Mints a **plain unique non-reserved idempotency key** server-side (e.g. `f"admin-{uuid4()}"`;
  the admin client does not supply one). The call reads `origin=null` and falls into the
  existing calls-list `origin=adhoc` bucket (NULL-or-non-reserved key).
- **DNC hard-blocks** → returns the `DNC_BLOCKED` call (HTTP 200) for the UI to surface;
  it is not a 4xx error.
- **Quiet-hours are not enforced server-side** (matches `POST /v1/calls`); the deliberate
  "outside their window" speed bump is a UI-side ack (§5, §6).

### 4.4 DNC — new `routers/admin_dnc.py`

| Endpoint | Verb | Role | Notes |
|---|---|---|---|
| `/v1/admin/dnc` | GET | VIEWER | **new** list read (masked phone, reason, created_at), paged |
| `/v1/admin/dnc` | POST | ADMIN | add: `phone_e164`, `reason?` (uses existing `lock_phone` + `add_entry`) |
| `/v1/admin/dnc/{phone_e164}` | DELETE | ADMIN | remove (E.164-validated path param; 404 if absent) |

Requires a new `repositories/dnc.list_entries(db, *, limit, offset)` read method.

## 5. PHI & safety design

- **Masked phone on edit (confirmed decision):** list/detail responses return a **masked**
  phone (existing `mask_phone` convention). Editing a number means submitting a
  *replacement* full E.164 value — the stored full number is never echoed back to the
  browser. This is a small, deliberate UX friction in service of PHI minimization.
- **Audit rows are PHI-free:** every write records `actor_email`, `action`, `entity_type`,
  `entity_id`, and a `detail` dict of ids/flags/counts only — never names, phones, or
  `dynamic_vars` values. Mirrors `admin_contacts` / `admin_calls`.
- **Call-now ack:** the UI requires a checked "this is outside their normal window"
  acknowledgement before enqueueing (the server does not block on window); DNC always
  hard-blocks regardless and is surfaced clearly.
- **Schedule `dynamic_vars`** are live re-used config (retention-exempt); the documented
  PHI-removal path (DELETE / PATCH-clear) is preserved.

## 6. Frontend (admin-ui, Phase 1)

React SPA under `apps/admin-ui`, Google-SSO session only, talking to `/v1/admin/*` +
`/v1/auth/*`. Follows existing feature-folder + vitest patterns.

- **`features/contacts` (extend):** create/edit drawer (name, phone, timezone,
  external_id, preferred_voice, metadata) + a contact detail view; phone field accepts a
  full replacement number, shows masked current value.
- **`features/schedules` (new):** a **Schedules tab on contact detail** (morning/evening
  slot, window, days-of-week, enable toggle, optional profile override + dynamic vars),
  plus a **global Schedules list** with the `skipped_window` "who missed" filter.
- **`features/dnc` (new):** simple list + add (phone + reason) + remove, with confirm on
  remove.
- **Call-now action:** a button on a contact row / contact detail → **confirmation modal**
  with the required "outside normal window" ack checkbox; surfaces `DNC_BLOCKED` and
  dispatch-unavailable (503) outcomes inline.
- Wire `routes.tsx` + nav; add typed client methods in `src/lib`.

## 7. Testing

- **API (pytest):** RBAC tiers (VIEWER read OK, non-ADMIN write → 403); **RLS isolation**
  (org A cannot read/write org B rows); audit-row shape is PHI-free; DNC hard-block path;
  `adhoc` origin on the minted idempotency key; one-schedule-per-slot and fail-closed
  window 422s; contact uniqueness 409s.
- **UI (vitest):** one test file per new/changed page, matching the existing
  `src/test/*.test.tsx` pattern (render, role-gated controls, call-now ack gating, DNC
  remove confirm).
- **Lint/types:** `ruff check . && ruff format .` and **`uv run mypy`** for both
  `apps/api` and `services/agent` are part of CI ("Lint Python"); run locally before push.
- Target ≥80% coverage on new modules.

## 8. Delivery & deploy

- **Two squash PRs (confirmed):**
  - **PR A — Phase 0 backend:** shared-service extraction + new/extended admin routers +
    `dnc.list_entries` repo + schemas + pytest. Independently mergeable; no UI dependency.
  - **PR B — Phase 1 admin-ui:** the four UI surfaces + client methods + vitest, stacked
    on PR A. Rebase onto `origin/main` after PR A squash-merges (per the repo's
    plan-PR workflow).
- **No new env keys** → no VM `.env` refresh required. Ships on a `v*` tag like all app
  code (merging to `main` alone changes nothing live).
- **No new DB migration** expected: all tables already exist and are tenant-scoped; the
  only schema-adjacent change is a new repository read method, not DDL. (Re-confirm during
  implementation — if a contact `DELETE` needs cascade/guard changes, that is called out
  in PR A.)

## 9. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Shared-service extraction subtly changes operator `POST /v1/calls` behavior | Extract behavior-preserving; keep/extend the existing operator-path tests as the regression guard before adding the admin path. |
| New admin endpoint forgets `get_tenant_db` → cross-org leak | RLS-isolation test per endpoint is a required, not optional, test. |
| Contact `DELETE` orphans calls/schedules or violates FKs | Honor existing in-use guards (cf. `ProfileInUseError` precedent); decide block-vs-cascade explicitly in PR A. |
| Masked-phone-on-edit confuses admins editing a typo | Accept as designed (PHI minimization); revisit only if it proves to be a real workflow blocker. |
| Call-now dispatch unavailable in environments without telephony | Surface the existing 503 ("outbound calling is not available") cleanly; not a new failure mode. |

## 10. Open items to confirm during planning

- Whether contact `DELETE` should hard-delete, soft-delete, or block-if-in-use.
- Final placement of schedules UI (contact-detail tab vs standalone page vs both) — spec
  assumes both (per-contact tab + global list).
