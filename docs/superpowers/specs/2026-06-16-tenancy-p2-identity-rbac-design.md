# P2 — Multi-Tenant Identity, Membership, RBAC & Super-Admin Act-As

**Date:** 2026-06-16
**Status:** Design (approved in brainstorming; pending spec review)
**Phase:** P2 of the multi-tenancy roadmap (builds on P1 — organizations + fail-closed Postgres RLS)
**Predecessor spec:** `docs/superpowers/specs/2026-06-16-tenancy-foundation-design.md`

## Goal

Make the **admin plane** genuinely multi-tenant: a person logs in, is scoped to the organization(s) they belong to, and sees/manages only that org's data. USAN staff get a global **super-admin** tier that can "act as" any client org with full write power, fully audited. This is the foundation that lets `admin.usanretirement.com` be opened to client organizations.

## Scope

### In P2
- Many-to-many **membership model** (`admin_users` global identity + `memberships(email, org_id, role)`).
- Org-scoped **login / session / org-switch**, including super-admin **act-as**.
- Per-request **re-validation** (instant membership/role/disable revocation).
- The **memberships management backend API** + an **org-create endpoint**.
- A **minimal admin UI**: org switcher, act-as enter/exit + persistent banner, members-management table, super-admin org console (create client orgs + add the first member).
- The two **HIGH items deferred from P1**: composite per-org uniqueness on RLS tables, and functional/endpoint tests running under the non-superuser `usan_app` role.

### Explicitly NOT in P2
- **P3 (onboarding):** polished email **invitations** (notification + token accept flow) and **self-service "create your own org" signup**.
- **Later phase:** **runtime/call-plane multi-org** — service-token (agent), Telnyx webhooks, and background pollers keep P1's single-`usan`-org behavior. Making *calls* multi-org needs call↔org routing (phone-number→org mapping, per-org trunks) that does not exist yet.
- **Later phase:** per-org SSO / per-org Google hosted-domain federation.

### The single most important boundary
**P2 isolates the admin plane (humans in the UI); the runtime/call plane stays single-org until a dedicated later phase.** This is safe because prod stays single-org until the `usan_app` DATABASE_URL cutover + real onboarding, and because clients drive only the admin plane (managing *their* contacts/profiles/schedules), not call placement.

## Key Decisions (from brainstorming)

| # | Decision |
|---|----------|
| D1 | **Many-to-many membership**: one email can belong to multiple orgs, each with its own role; the session carries an "active org"; the UI has an org switcher. |
| D2 | **Auth = Google OAuth + invite-driven allowlist.** Client admins sign in with Google; access is governed by `admin_users`/`memberships`, not a Google hosted-domain. Invited emails must be Google-signinable. |
| D3 | **Super-admin tier is global** (USAN staff); can **act-as** any org with **full write (org-ADMIN powers)**, audit-logged with the real identity + a persistent banner. |
| D4 | **Active org + act-as live in the stateless session JWT** (Approach A) — no server-side session store; switching re-issues the cookie; the DB is re-checked every request. |
| D5 | **Control-plane tables stay global / non-RLS** (`admin_users`, `memberships`, `organizations`); all PHI/config tables stay RLS-scoped from P1. App code scopes control-plane queries by active org. |
| D6 | **In P2, super-admins create client orgs and add the first member** (orgs are not self-service yet — that's P3). |

## Architecture

### 1. Data model

Three **global, non-RLS control-plane tables** (must be readable before an org context exists, e.g. to list a user's orgs at login):

**`admin_users`** — the global identity, one row per person:
- `email` (PK, lowercase) — unchanged
- `is_super_admin` (bool, NOT NULL, default `false`) — **new**; the USAN-staff global tier
- `status` (enum `active`|`disabled`, NOT NULL, default `active`) — **new**; disable a person across all orgs at once
- `last_active_org_id` (uuid, NULL, FK → organizations) — **new, optional**; remembers the last selected org for multi-org login
- `created_at` — unchanged
- ⚠️ the per-org `role` column is **removed** — role is now per-membership

**`memberships`** — **new**, the many-to-many join:
- `email` (FK → `admin_users.email`, ON DELETE CASCADE)
- `organization_id` (FK → `organizations.id`, ON DELETE CASCADE)
- `role` (enum `ADMIN`|`VIEWER`, reusing the existing `AdminRole`) — scoped to *this* org
- `added_by` (text, NULL), `created_at`
- **composite PK `(email, organization_id)`** — one membership per person per org
- granted to `usan_app`; **not** RLS-enabled

**`organizations`** — from P1, unchanged.

### 2. Authentication & session flow

**Google OAuth is unchanged** (login → callback → verify ID token → extract verified email). The post-login logic changes:

**`/v1/auth/callback`:**
1. `admin_users[email]` must exist and be `active` (else deny + audit, as today).
2. Resolve the email's `memberships` (super-admins included — being a super-admin is orthogonal to having memberships):
   - **1 membership** → `active_org` = that org.
   - **>1 memberships** → land on org picker; `active_org` defaults to `last_active_org_id` or the first.
   - **0 memberships:** non-super-admin → deny ("you haven't been invited to any organization"); super-admin → allowed in with `active_org` null, landing on the super-admin console to pick an org to act-as.
3. Issue the `admin_session` cookie.

**Session JWT claims** (extends today's `sub`/`role`/`typ`): `sub` (email), `active_org_id` (nullable), `role` (role in the active org), `is_super_admin`, `acting_as` (true when `active_org` came from act-as rather than a membership), `iat`/`exp`.

**Org switch + act-as — one endpoint, `POST /v1/auth/switch-org {organization_id}`:**
- Has a **membership** in that org → re-issue with that org + its role, `acting_as=false`.
- Else `is_super_admin` → **act-as**: re-issue with that org, `role=ADMIN`, `acting_as=true`, and **audit-log the act-as entry** (real email + target org).
- Else → 403.
- On success, update `last_active_org_id`.

**Per-request re-validation** — `require_admin_session` (extended), preserving instant revocation:
- Decode session → re-check `admin_users[email]` exists + `active`.
- If `acting_as` → require `is_super_admin` still true (else 401).
- Else → require a **live `membership(email, active_org_id)`** and read `role` from it.
- Build `AdminPrincipal(email, active_org_id, role, is_super_admin, acting_as)`.

**Smaller changes:**
- The single global hosted-domain (`hd`) restriction relaxes to **allowlist-only**.
- `/v1/auth/me` returns `active_org` (id + name), `is_super_admin`, `acting_as`, and the user's org list (for the switcher).

### 3. Org resolution — the request seam

**Circular-dependency note:** auth needs the DB before the org is known. Resolved because `admin_users`/`memberships` are **non-RLS global tables** — `require_admin_session` reads them on its own ephemeral session with no org context.

**Admin request path (heart of P2):**
- New dependency **`get_tenant_db`** — depends on `require_admin_session`, opens the request session, calls `set_tenant_context(session, principal.active_org_id)`. The ~15 `admin_*` routers swap `Depends(get_db)` → `Depends(get_tenant_db)`.
- Super-admin with no active org hitting an org-scoped route → `409 "select an organization first"`.
- `resolve_default_org_id` stays (used by the runtime/worker path below).

**Runtime/worker path (kept single-org in P2):**
- Service-token routes (agent / `runtime` / `tools`) → keep `get_db` (default org).
- Telnyx webhook routes → keep `get_db` (default org).
- Background pollers → keep the P1 connect-time default-org baseline (`_install_default_org_context`).

### 4. Authorization & act-as enforcement

- `require_admin_role(ADMIN)` unchanged in shape; `principal.role` now comes from the active-org membership. One email can be `ADMIN` in org Y and `VIEWER` in org X.
- Super-admin acting-as → `role=ADMIN` in the target org.
- **Audit & accountability:** the recorded `actor` is **always the real super-admin email**, never the impersonated org. Act-as actions write to the **target org's** `AdminAuditLog` (RLS-scoped) with `acting_as=true`, so the client's own audit trail transparently shows "USAN staff X did Y on your behalf." The act-as entry event is logged the same way.
- **Memberships API authorization:** an org `ADMIN` manages memberships within their active org; `VIEWER` cannot; super-admin can manage any org. Because these tables are non-RLS, the repo **must** scope every query to `active_org_id` in app code — the mandatory guard replacing RLS for control-plane tables.

### 5. Migration & bootstrap

A new migration (`0033+`, following P1's `0032`):
1. `admin_users`: add `is_super_admin` (default false), `status` (default active), `last_active_org_id` (nullable FK); keep `email` PK.
2. Create `memberships` (composite PK `(email, organization_id)`, FKs, global/non-RLS, granted to `usan_app`).
3. **Data migration:** for each existing `admin_users` row, insert `membership(email, <usan org>, role=current role, added_by='migration')`, then **drop `admin_users.role`**. Existing operators keep their exact `usan` access.
4. `ADMIN_BOOTSTRAP_EMAILS` seeding (`_seed_admin_allowlist`) now sets `is_super_admin=true` **and** ensures a `usan` `ADMIN` membership.

Plus the two P1-deferred HIGH items:
- **Composite per-org uniqueness:** a fan-out migration (like P1's `0032`) converting globally-unique natural keys on RLS tables to `UNIQUE(col, organization_id)`; the specific columns are enumerated during planning.
- **Functional tests under `usan_app`:** the test `client` fixture connects as the non-superuser role and sets org context, so endpoint tests exercise RLS rather than bypassing it as superuser.

### 6. Admin UI (minimal, P2)

- **Org switcher** in the nav (visible when the user has >1 org or is a super-admin).
- **Act-as**: a super-admin console listing all orgs; entering act-as shows a persistent **"You are acting as <Org>"** banner with an exit control.
- **Members-management page**: an org ADMIN lists/adds (by email)/role-changes/removes members within the active org.
- **Super-admin org console**: create a client org + add its first member.
- Deferred to P3: email invitations and self-service org signup.

## Error Handling

| Code | When |
|------|------|
| 401 | No/invalid/expired session; `admin_users` disabled or removed; `acting_as` but no longer super-admin |
| 403 | Role too low for the action; no membership in the active org (and not super-admin) |
| 404 | Unknown org on `switch-org` |
| 409 | Super-admin with no active org selected on an org-scoped route |

Messages are user-friendly and never leak whether another org exists or who belongs to it.

## Testing

Extends P1's fail-closed isolation suite:
- **Two-org isolation:** admin with a membership only in A sees only A; switching to B without a membership → 403; a member of both switches cleanly.
- **Act-as:** super-admin (no B-membership) switches to B, gets full write, actions land in **B's** audit log with the real email + `acting_as`.
- **Instant revocation:** deleting a membership / setting `status=disabled` is enforced on the next request.
- **Composite uniqueness:** same natural key inserts in A and B; duplicate within one org fails.
- **Deferred-item payoff:** endpoint tests run **as `usan_app` under RLS**, proving a handler in org A can't read org B even by guessing IDs.
- **Membership API & session:** org-ADMIN-only management, VIEWER blocked, cross-org blocked; 0/1/many-membership login resolution; `/me` returns the org list.

## Implementation Units (for the plan)

Each unit is spec+quality reviewed under subagent-driven development:
- **A** — schema + migration + models (`admin_users` alter, `memberships`, data migration, bootstrap update).
- **B** — auth/session (membership resolution, session claims, `switch-org`/act-as, `require_admin_session` + `get_tenant_db`, `/me`).
- **C** — memberships CRUD API + app-layer org scoping + org-create endpoint.
- **D** — composite per-org uniqueness migration + functional tests under `usan_app`.
- **E** — admin UI (org switcher, act-as banner, members page, super-admin org console).

## Risks & Mitigations

- **Control-plane tables are non-RLS** → app-layer scoping is mandatory; mitigated by always filtering `memberships`/`admin_users` queries by `active_org_id` and covering it with cross-org API tests.
- **Act-as is the sharpest tool** → mitigated by full audit (real identity always recorded), the persistent banner, and `is_super_admin` re-checked every request.
- **Two sessions per admin request** (ephemeral auth lookup + org-scoped request session) → minor cost; the auth session only reads small global tables.
- **Prod activation still pending** → P2 is behavior-preserving until the `DATABASE_URL` → `usan_app` cutover; the `_check_rls_role_capability()` startup guard from P1 still warns if the live role can bypass RLS.
