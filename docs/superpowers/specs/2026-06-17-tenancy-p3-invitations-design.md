# P3 — Org Invitations (Pending-Token, Copyable-Link)

**Date:** 2026-06-17
**Status:** Design (approved in brainstorming)
**Phase:** P3 of the multi-tenancy roadmap (builds on P1 RLS foundation + P2 identity/RBAC/act-as)
**Predecessor specs:** `2026-06-16-tenancy-foundation-design.md`, `2026-06-16-tenancy-p2-identity-rbac-design.md`

## Goal

Let an organization **ADMIN** invite a person by email into *their* org through a
**pending-token invitation** with a **copyable accept link**. The invitee opens the
link, signs in with Google, and — if the Google-verified email matches the invite — is
added to the org with the granted role. This is the onboarding layer on top of P2: it
turns P2's manual "super-admin adds a member" into a self-serve, org-ADMIN-driven flow,
**without** opening public self-service org creation.

## Scope

### In P3
- A new **`invitations`** control-plane table + repository (create / list / get-by-token /
  revoke / resend / accept-consume), with all guards.
- The **invite-management API**: `POST/GET/DELETE /v1/admin/invites` + resend, ADMIN-only,
  scoped to the caller's active org.
- **Copyable-link delivery**: the create/list/resend responses return an `accept_url`; the
  admin shares it out-of-band. **No email is sent** (no provider, no new vendor/BAA).
- The **invite-aware OAuth accept flow**, including the **brand-new-invitee bypass**: a
  valid pending invite authorizes a person who is *not* on the bootstrap allowlist and has
  *no* prior membership to pass through Google OAuth and join the org.
- A **minimal admin UI**: a "Pending invites" section on the existing Members page
  (invite / copy link / resend / revoke) and a public `/accept-invite?token=…` route.
- Tests, including **isolation under the non-superuser `usan_app` role**.

### Explicitly NOT in P3
- **Auto-sending invite email.** Deferred. The data model and accept flow are unchanged
  when this is added later — only a "send" step is added on top of `accept_url`.
- **Self-service org creation / public signup.** Deferred. Creating new orgs stays
  **super-admin-only** (P2's org console). P3 only invites people into *existing* orgs.
- **Per-org SSO / hosted-domain federation.** Unchanged from P2.
- **Runtime/call-plane multi-org.** Still deferred (service-token, webhook, poller paths
  keep P1/P2 single-`usan`-org behavior).

### The single most important boundary
**A valid pending invite + a matching Google login is the authorization.** The invite
*token* is **not** a secret: Google OAuth plus an exact email match is the security gate.
The token only (a) lets the accept link find the right invite and (b) carries lifecycle
state (pending / expiry / revoke / resend). Even a leaked token is useless without control
of the exact Google account it was issued to.

## Key Decisions (from brainstorming)

| # | Decision |
|---|----------|
| D1 | **Invite-only onboarding.** Org creation stays super-admin-only; P3 invites people into existing orgs. |
| D2 | **Pending-token lifecycle.** A new `invitations` table; statuses `pending`/`accepted`/`revoked`; expiry is **derived** from `expires_at` (no sweeper/cron). |
| D3 | **Copyable-link delivery.** The API returns `accept_url`; the admin shares it. No email provider, secret, or BAA. |
| D4 | **The token is not the security boundary.** Google OAuth + exact email match is. The token is high-entropy and re-copyable, but treated as non-secret. |
| D5 | **A valid pending invite authorizes a brand-new (non-allowlisted) person** through OAuth. The `ADMIN_BOOTSTRAP_EMAILS` allowlist stays **super-admin-only**; normal members are authorized by invites. |
| D6 | **`invitations` is a GLOBAL, non-RLS control-plane table** (it must be looked up by token *before* the accepter has any org context — the same reason `memberships`/`admin_users` are non-RLS). App code scopes every *management* query by `active_org_id`; accept looks up by the globally-unique token. |

## Architecture

### 1. Data model — `invitations` (migration `0035`)

Joins `admin_users` / `memberships` / `organizations` as a **global, non-RLS control-plane
table**. Granted to `usan_app`; **not** RLS-enabled. App code is responsible for scoping
management queries by `organization_id` (the mandatory guard that replaces RLS for
control-plane tables, per P2 §D5).

| column | type / notes |
|---|---|
| `id` | uuid PK (default `gen_random_uuid()`) |
| `organization_id` | uuid NOT NULL, FK → `organizations.id` ON DELETE CASCADE |
| `email` | text NOT NULL, stored lowercased (the invitee) |
| `role` | enum `AdminRole` (`ADMIN`\|`VIEWER`) — granted on accept |
| `token` | text NOT NULL, **UNIQUE** — `secrets.token_urlsafe(32)` |
| `status` | enum `invite_status` (`pending`\|`accepted`\|`revoked`), NOT NULL, default `pending` |
| `invited_by` | text NULL (the creating actor's email) |
| `created_at` | timestamptz NOT NULL, default now |
| `expires_at` | timestamptz NOT NULL |
| `accepted_at` | timestamptz NULL |

Constraints / indexes:
- `UNIQUE (token)` — accept looks up globally by token.
- **Partial unique** `(organization_id, lower(email)) WHERE status = 'pending'` — at most
  one *live* invite per email per org (re-inviting regenerates the existing pending row).
- index on `(organization_id)` for the management list.
- **Expiry is derived**: an invite is usable iff `status = 'pending' AND expires_at > now()`.
  No background job marks invites expired; the accept path and the UI compute it lazily.

The `invite_status` enum is created in this migration. `role` reuses the existing
`AdminRole` PG enum (do **not** re-emit `CREATE TYPE`; use
`postgresql.ENUM(name="adminrole", create_type=False)` exactly as migration `0033` did).

### 2. Settings

- `invite_ttl_hours: int = 168` (7 days), validated `ge=1, le=720`, alias `INVITE_TTL_HOURS`.
- **Accept-URL origin.** `admin_post_login_redirect` is a *relative* path and there is no
  absolute admin-origin setting today. Add `admin_base_url: str | None = None`
  (alias `ADMIN_BASE_URL`, absolute `http(s)://…` when set). The `accept_url` origin is
  `admin_base_url` when set, else the **origin of `GOOGLE_OAUTH_REDIRECT_URI`** (already
  configured for SSO). This means prod needs **no new required env var** — the fallback
  derives the public origin from existing config. (If `ADMIN_BASE_URL` is later set in prod,
  it must be added to BOTH the compose `api` `environment:` map AND the VM `.env`, per the
  compose-env-passthrough rule.)

`accept_url = f"{origin}/v1/auth/accept-invite?token={token}"` — it points at the API
accept endpoint (served from the same public origin as the SPA via Caddy's `/v1` proxy).

### 3. Invite-management API — `routers/admin_invites.py`

Mounted at `/v1/admin/invites`, `dependencies=[Depends(require_admin_session)]`, every
write gated by `require_admin_role(AdminRole.ADMIN)` and scoped to `principal.active_org_id`
via `get_tenant_db`. An invite belonging to another org is invisible here → `404`.

- **`POST /v1/admin/invites {email, role}` → 201**
  `InviteOut {id, email, role, status, accept_url, expires_at, created_at, invited_by}`.
  - `409` if `email` is already a **member** of the active org.
  - If a `pending` invite already exists for `(active_org, email)` → **regenerate** its
    token + `expires_at` and return it (idempotent re-invite), rather than erroring.
  - Audit `invite.create` in the target org.
- **`GET /v1/admin/invites` → `list[InviteOut]`** — the active org's **pending** invites
  (with `accept_url`, so the admin can re-copy). ADMIN-only.
- **`DELETE /v1/admin/invites/{id}` → 204** — set `status = revoked`. ADMIN-only.
  `404` if not in the active org; `409` if not `pending`. Audit `invite.revoke`.
- **`POST /v1/admin/invites/{id}/resend` → `InviteOut`** — regenerate the token + reset
  `expires_at`; return the new `accept_url`. ADMIN-only; `409` if not `pending`. Audit
  `invite.resend`.

### 4. Accept flow — `routers/auth.py` additions + repo

Uniform for brand-new **and** existing users (an already-logged-in user re-auths through
Google — one path, no second endpoint).

- **`GET /v1/auth/accept-invite?token=…`** — `400` if the token is missing/blank.
  Otherwise: set a **short-lived signed invite cookie** (`INVITE_COOKIE`, ~10 min, same
  signing approach as the OAuth `tx` cookie) carrying only the token, **and** begin the
  normal OAuth `tx` + redirect to Google (reusing the `/login` machinery).
- **`GET /v1/auth/callback`** branches on the presence of the invite cookie:
  - **Invite cookie present** → after verifying the Google email, load the invite by token
    (global, non-RLS). Require: `status = pending`, `expires_at > now()`, and
    `invite.email == verified_email`. On success:
    1. `set_tenant_context(db, invite.organization_id)` (so the audit row is RLS-scoped to
       the target org),
    2. `ensure_identity(db, email=verified_email)` (creates the `admin_users` row if the
       invitee is brand-new — **this is the allowlist bypass**),
    3. `add_member(db, email=verified_email, org_id=invite.organization_id, role=invite.role, added_by=f"invite:{invite.invited_by}")`,
    4. mark the invite `accepted` (`accepted_at = now`, `status = accepted`),
    5. audit `invite.accept` (target org) + `auth.login`,
    6. issue a session **scoped to the invite's org** at the granted role, `acting_as=False`,
    7. redirect (303) to `admin_post_login_redirect` (lands in the joined org). Clear the
       invite + tx cookies.
  - **Invite present but invalid** (mismatched email / expired / revoked / accepted /
    unknown token) → **do NOT consume**; clear cookies; audit `invite.accept_denied`
    (target org when resolvable, with both emails in `detail`); redirect the browser to
    `{accept-url-origin}/accept-invite?status=error&reason=<code>` so the SPA shows a
    friendly message (`mismatch` / `expired` / `revoked` / `invalid`).
  - **No invite cookie** → existing behavior unchanged (the P2 allowlist path).
- **Idempotency / edge cases:**
  - The verified email is **already a member** of the invite's org (double-accept, or
    invited to an org they already belong to) → treat as success: if the invite is still
    `pending`, mark it `accepted`; issue a session into that org. No duplicate membership
    (the `add_member` upsert is idempotent).
  - The invitee is **already an `admin_user`** with memberships in *other* orgs → the
    membership for the invite's org is simply added (P2's many-to-many model).

### 5. Authorization summary

- **Invite create / list / revoke / resend:** org `ADMIN` in the active org, or a
  super-admin **acting-as** that org (act-as grants ADMIN). `VIEWER` → `403`.
- **Accept:** governed solely by the token + Google email match. No prior membership,
  allowlist entry, or session is required — that is the point of onboarding.

### 6. Admin UI (minimal, P3)

- The existing **Members page** gains a **"Pending invites"** section:
  - an **Invite** control (email + role select) → `POST /v1/admin/invites`; on success show
    the `accept_url` with a **Copy link** button.
  - a pending-invites table: email, role, invited-by, expires (relative), with per-row
    **Copy link**, **Resend**, **Revoke** actions.
  - Visible/actionable only to an org ADMIN (mirror the members-management gating).
- A new **public route `/accept-invite`**:
  - With `?token=…` and not yet accepted → a short page that forwards the browser to
    `GET /v1/auth/accept-invite?token=…` (which bounces through Google). On return the user
    lands in the joined org.
  - With `?status=error&reason=…` → a friendly error message (mismatch / expired / revoked /
    invalid) with a link back to login.

## Error Handling

| Code | When |
|------|------|
| 400 | Missing/blank token on `accept-invite`. |
| 403 | `VIEWER` on any invite-management endpoint; non-super non-member reaching org-scoped routes. |
| 404 | Invite `id` not found in the active org (revoke/resend). |
| 409 | Invite create when the email is already a member; revoke/resend when the invite is not `pending`. |
| (redirect) | Accept failures (mismatch/expired/revoked/invalid) redirect to the SPA `/accept-invite?status=error&reason=…`; success redirects to `ADMIN_POST_LOGIN_REDIRECT`. |

Messages never leak whether another org exists or who belongs to it.

## Testing

Extends P2's fail-closed isolation + membership suites:
- **Repo:** create (new + regenerate-on-existing-pending); already-a-member guard;
  get-by-token; revoke (`pending`→`revoked`, non-`pending`→error); resend (rotates token +
  `expires_at`); accept-consume (valid → membership + `accepted`; expired / revoked /
  mismatch → no consume); lazy expiry (`expires_at` in the past → not usable).
- **API:** ADMIN-only (VIEWER `403`); cross-org isolation (`404` for another org's invite);
  create returns a working `accept_url`; idempotent re-invite; audit rows written.
- **Accept flow (integration):** brand-new user (no `admin_users`, no membership) accepts →
  gets identity + membership + a session scoped to the org; existing-other-org user accepts
  → membership added; email mismatch → denied, **not** consumed; expired → denied; revoked →
  denied; already-a-member → idempotent success.
- **Isolation under `usan_app`:** an org-A invite is invisible/unmanageable from org B;
  accepting an org-A invite creates a membership only in A.
- **UI:** invite create surfaces the copy link; resend/revoke call the right endpoints;
  the accept route forwards correctly; vitest with `vi.mock("../lib/api")` (no MSW).

## Implementation Units (for the plan)

Each unit is spec + quality reviewed under subagent-driven development:
- **A** — schema + model + migration `0035` + `invitations` repo (create / list / get-by-token /
  revoke / resend / accept-consume, with all guards) + `invite_ttl_hours`/`admin_base_url`
  settings.
- **B** — invite-management API (`admin_invites.py`: create / list / revoke / resend) under
  `get_tenant_db` + `require_admin_role(ADMIN)`, returning `accept_url`.
- **C** — accept flow: `accept-invite` endpoint (invite cookie + OAuth bounce) + the
  invite-aware `callback` branch + brand-new-user bypass + the accept-URL builder.
- **D** — admin UI (pending-invites section on the Members page + public `/accept-invite`
  route).
- **E** — tests: repo, API authz/isolation, accept flow, isolation under `usan_app`, UI.

## Risks & Mitigations

- **`invitations` is non-RLS** → app-layer scoping by `active_org_id` is mandatory;
  mitigated by cross-org API + `usan_app` isolation tests.
- **The invite is an allowlist bypass** (intentional, but the sharpest tool here) →
  mitigated by: exact email match against the **Google-verified** email; single-use
  (`status` flips to `accepted`); pending + unexpired required; ADMIN-gated creation;
  super-admin re-checked every request; full audit of `create` / `accept` / `accept_denied`.
  A typo'd invite can only ever be accepted by the controller of that exact Google account.
- **Token stored in the DB** is non-secret but high-entropy and ADMIN-gated on read; even
  leaked, it is useless without the matching Google account.
- **Prod is still pre-cutover (RLS inert)** → P3 being invite-only keeps the blast radius to
  trusted, super-admin-created orgs; there is no public signup. Real isolation still depends
  on the `DATABASE_URL` → `usan_app` cutover; the P1 `_check_rls_role_capability()` startup
  guard still logs CRITICAL if the live role can bypass RLS.
