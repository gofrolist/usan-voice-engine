# P4 — Client Portal (design)

**Date:** 2026-06-17
**Roadmap:** Multi-tenancy phase 4 of 6. Builds on P1 (RLS foundation), P2 (org identity / RBAC / act-as), P3 (org invitations).
**Prior specs:** `2026-06-16-tenancy-foundation-design.md`, `2026-06-16-tenancy-p2-identity-rbac-design.md`, `2026-06-17-tenancy-p3-invitations-design.md`.

## 1. Goal

Open the existing admin app (`admin.usanretirement.com`) to **client-organization users** by sharpening role gates so client ADMIN/VIEWER users see only client-appropriate surfaces, and **operator-only surfaces are hidden** from them. Give a client ADMIN a read view of their **own org's audit log**. Prove tenant isolation by onboarding a real second org.

P4 is the "client-facing app/routes and hiding operator-only surfaces" phase named in the foundation spec (§2). It is an **admin-plane** change only: the runtime/call plane stays single-org (see §3).

This is **Approach A — same app, role-gated**: one `admin-ui` build, no separate client SPA. P1–P3 already shipped the load-bearing plumbing (fail-closed RLS, `get_tenant_db`, the `AdminPrincipal` session with `active_org_id`/`role`/`is_super_admin`/`acting_as`, role-gated nav, org switcher, act-as banner, members/invites). P4 mostly **re-tags which surfaces each tier sees, adds matching backend gates, route guards, a role-aware landing, and an isolation test suite** — not net-new subsystems.

## 2. Non-goals (deferred — do NOT build here)

- **Per-org outbound calling.** Phone-number→org routing, per-org Telnyx trunks, and background-poller org-iteration stay deferred to the later runtime/call-plane multi-org phase. In P4 a newly onboarded org can log in and manage its admin-plane data, but **its contacts are not dialed** — calling continues to operate as the single default org. This is the deliberate boundary.
- **Client profile authoring.** Profiles (agent prompt/voice/tool config) stay **operator-only**. Client ADMINs do not create, edit, or publish profiles.
- **Client self-serve contact creation/import.** Contact provisioning stays on the operator plane (USAN onboards a client's contacts). Clients view + edit existing contacts (assign profile, set timezone) only.
- **Per-org SSO / Google hosted-domain federation.** Unchanged from P2.
- **PHI export / per-tenant PHI access reporting / per-org BAAs / key management** — P6.
- **Edge hardening / WAF / removing the operator-CIDR gate** — P5. The portal remains behind the operator CIDR allowlist in P4.
- **Billing / quotas / plans** — possible P7.
- **The operator-key / runtime plane is untouched.** `contacts.py` (create), `dnc.py`, `webhook_endpoints.py`, `batches.py`, `schedules.py`, `calls.py`, `tools.py`, `runtime.py`, `webhooks.py` keep their existing `OPERATOR_API_KEY` / service-token auth and single-org behavior. They are not in the admin UI and P4 does not expose them to clients.

## 3. The three effective tiers

| Tier | Identity | Sees |
|---|---|---|
| **Operator** | `is_super_admin = true` (USAN staff) | Everything, in its own org or any org via act-as |
| **Client ADMIN** | membership `role = ADMIN`, not super-admin | Client surfaces + member/invite management for its org |
| **Client VIEWER** | membership `role = VIEWER`, not super-admin | Read-only client surfaces |

"Operator-only" maps onto the **existing** `require_super_admin` dependency (`auth.py:280-292`) on the backend and the **existing** `superAdminOnly` nav flag (`NavSidebar.tsx`) on the frontend. In this product USAN staff *are* the operators, so `is_super_admin` is the operator boundary. A super-admin acting-as a client org still sees operator surfaces for that org (intended — operators configure a client via act-as; RLS scopes the data to the act-as target).

## 4. Surface matrix

Definition of "Change?": relative to the current (P3) gating.

| Surface | Route(s) | Operator | Client ADMIN | Client VIEWER | Change? |
|---|---|---|---|---|---|
| Profiles (list / editor / version history) | `/`, `/profiles/:id`, `/profiles/:id/versions` | ✓ | ✗ | ✗ | **→ operator-only** (was all roles) |
| Defaults | `/defaults` | ✓ | ✗ | ✗ | **→ operator-only** (was all roles) |
| Custom Variables | `/custom-variables` | ✓ | ✗ | ✗ | **→ operator-only** (was all roles) |
| Contacts (view + assign profile / set timezone) | `/contacts` | ✓ | ✓ | ✗ | keep (ADMIN-gated); create stays operator-key |
| Calls + detail (history, transcript, recording) | `/calls`, `/calls/:id` | ✓ | ✓ | ✓ | keep |
| Queues (follow-up flags, callback requests) | `/queues` | ✓ | ✓ (act) | ✓ (read) | keep |
| Audit (own org) | `/audit` | ✓ | ✓ | ✗ | **→ ADMIN-only** (was all roles) |
| Members + Invites | `/members` | ✓ | ✓ | ✗ | keep (ADMIN-gated) |
| Organizations console | `/organizations` | ✓ | ✗ | ✗ | keep (super-admin only) |

**Operator-only catalogs:** the profile-authoring reference endpoints (`admin_voice_catalog`, `admin_model_catalog`, `admin_tool_catalog`, `admin_variable_catalog`) are only consumed by the operator-only Profiles editor, so they move operator-only with it.

**Default landing:** `/` currently renders the Profiles list. Since clients can't see Profiles, the index route resolves by tier:
- Operator → Profiles (`/`, unchanged).
- Client ADMIN / VIEWER → **`/calls`** (call history is the most universal client view).

## 5. Backend changes (`apps/api`)

Defense-in-depth: hiding a nav item is not enough — every operator-only endpoint must enforce the gate so a client can't reach it by direct request.

1. **Operator gate on operator-only routers.** Swap the router-level dependency from `require_admin_session` / `require_admin_role(AdminRole.ADMIN)` → **`require_super_admin`** (keep `get_tenant_db` for RLS scoping) on: `admin_profiles`, `admin_defaults`, `admin_custom_variables`, `admin_profile_tests`, `admin_voice_catalog`, `admin_model_catalog`, `admin_tool_catalog`, `admin_variable_catalog`. A super-admin always has an `active_org_id` when operating (own org or act-as target), so `get_tenant_db` still resolves; a super-admin sitting on the org console with `active_org_id = None` picks an org first (existing behavior).
2. **Audit endpoint** (`admin_audit`): tighten from all-roles to **ADMIN+** (`require_admin_role(AdminRole.ADMIN)`), confirm it is strictly org-scoped via `get_tenant_db` (RLS on `admin_audit_log.organization_id`), and confirm act-as rows (written with `acting_as=true` to the **target** org) surface to the client so their trail transparently reads "USAN staff X did Y on your behalf." No row filtering is added — a client sees all audit rows for its own org.
3. **Isolation test suite** (extends the P2/P3 `usan_app` non-superuser tests): for a seeded client ADMIN of org B —
   - every operator-only endpoint returns **403** (not 404, not data);
   - `admin_audit` / `admin_contacts` / calls / queues return **only org B's rows**, never org A's (RLS proof);
   - a client VIEWER gets **403** on ADMIN-only surfaces (members, invites, audit);
   - act-as by a super-admin into org B writes audit rows visible to org B's ADMIN with the real super-admin email.

No schema migration is required — `admin_audit_log.organization_id` already exists (P1). No new tables.

## 6. Frontend changes (`apps/admin-ui`)

1. **Re-tag nav** (`NavSidebar.tsx`): Profiles → `superAdminOnly`; Defaults → `superAdminOnly`; Custom Variables → `superAdminOnly`; Audit → `adminOnly`. (Contacts/Members already `adminOnly`; Organizations already `superAdminOnly`; Calls/Queues stay all-roles.)
2. **Role-aware default landing**: the index route (`routes.tsx`) redirects by tier — operator → Profiles, client → `/calls`. Implemented as a small redirect component reading `useSession`.
3. **Route guards, not just nav hiding**: client deep-links to operator-only routes (`/`, `/profiles/*`, `/defaults`, `/custom-variables`, `/organizations`) are blocked/redirected client-side to mirror the backend 403, so a hand-typed URL can't render an operator page shell. A shared `<RequireTier>` wrapper (operator / admin) around the relevant routes.
4. **Client polish**: friendly empty states for a freshly-onboarded org (no calls/queues/contacts yet, because calling stays single-org in P4) so a new client sees guidance, not a broken/empty grid; show the active org name in the header for context.
5. **Vitest coverage**: nav visibility for operator / client-admin / client-viewer fixtures; the landing redirect per tier; the route guards (a client hitting `/profiles/x` is redirected).

## 7. Audit read surface (resolves P1 Open Question 2)

P1 added `admin_audit_log.organization_id` and deferred the read surface to P4. P4 decision: **client ADMINs read their own org's audit log** (RLS-scoped), VIEWERs do not, operators read any org they're in. Platform/act-as actions already write to the relevant org with `acting_as=true`, which is exactly what a client should see ("USAN staff did Y on your behalf"). Genuinely platform-global actions (e.g. creating the org, adding its first member from the super-admin console) execute with the new org as the active context and therefore appear in that org's trail — acceptable and arguably desirable ("your org was provisioned by USAN"). This is a verification point in §5.3, not extra code.

## 8. Onboarding & validation (operator-gated)

Like prior plans, the live validation is a manual operator step, not automated:
1. Via the super-admin Organizations console, create a second org and add/invite its first ADMIN (P2/P3 flows).
2. Sign in as that client ADMIN; confirm the surface matrix (§4) — operator surfaces absent, client surfaces present, audit scoped to the new org.
3. Confirm a client VIEWER sees the read-only subset.
4. Confirm the existing default org (USAN) is unchanged for operators, and that the new org's data never appears in USAN's views and vice versa.

## 9. Risks & mitigations

- **Nav-only hiding is not security.** Mitigated by §5 backend gates + §6.3 route guards + §5.3 isolation tests — the 403 is enforced server-side; the UI hide is cosmetic.
- **Control-plane tables are non-RLS** (`admin_users`, `memberships`, `organizations`, `invitations`). Unchanged P2/P3 risk; all queries scope by `active_org_id` in app code, covered by isolation tests.
- **Coarse operator boundary.** Making Profiles/Defaults/Variables `is_super_admin`-only means any USAN person who needs profile access must be a super-admin. Accepted for P4; a future per-org "can author profiles" capability is out of scope (YAGNI).
- **Empty portal for a 2nd org.** Because calling stays single-org, a new client org's portal has no call data. Mitigated by clear empty states (§6.4) and by setting expectations: P4 opens the doors; the runtime-multi-org phase makes the calls flow.
- **Audit over-exposure.** A client seeing act-as rows is intended; verified in §5.3 that no *other* org's rows leak and that the recorded actor is always the real super-admin email.

## 10. Open questions

1. **Client default landing** — `/calls` (proposed) vs `/queues` (the clinical actionable surface). Trivial to flip; default to `/calls`.
2. **VIEWER breadth** — VIEWER currently sees Calls + Queues (read). Should a VIEWER also see Contacts (read-only)? Kept ADMIN-only for now to limit PHI exposure; revisit if client clinical staff need contact context.
