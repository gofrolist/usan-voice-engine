# P2 Admin UI (Unit E) — Implementation Plan (PR 2)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`. This is PR 2 of P2, stacked on `feat/tenancy-p2-identity-rbac` (the backend, PR #100). Branch: `feat/tenancy-p2-ui`.

**Goal:** Admin UI for the P2 backend — org switcher, "acting as" banner, per-org members management (replacing the old global admin-users page), and a super-admin org console.

**Stack:** React 18 + TypeScript, react-router-dom (`createBrowserRouter`), @tanstack/react-query v5, Tailwind, vitest + @testing-library/react (NO MSW — mock `lib/api` directly). `apps/admin-ui`.

**CI gates (must pass):** `cd apps/admin-ui && npm test` (vitest run), `npm run lint` (eslint `--max-warnings 0`), `npm run typecheck` (tsc `--noEmit`).

---

## EXACT backend contract (the source of truth — do NOT invent endpoints/shapes)

These are the real endpoints shipped in PR #100. Earlier scouting guessed some wrong; use ONLY these.

| Method | Path | Body | Returns |
|--------|------|------|---------|
| GET | `/v1/auth/me` | — | `Me` |
| POST | `/v1/auth/switch-org` | `{ organization_id: string }` | `Me` (partial: `orgs: []`) |
| GET | `/v1/admin/members` | — | `Member[]` (active org) |
| POST | `/v1/admin/members` | `{ email, role }` | `Member` (201) |
| PATCH | `/v1/admin/members/{email}` | `{ role }` | `Member` |
| DELETE | `/v1/admin/members/{email}` | — | 204 |
| GET | `/v1/admin/organizations` | — | `Organization[]` (super-admin only; 403 else) |
| POST | `/v1/admin/organizations` | `{ name, slug, first_admin_email? }` | `Organization` (201; 409 dup slug; 422 bad slug) |

**Types to add/replace in `apps/admin-ui/src/types/api.ts`** (`AdminUserRole = "admin" | "viewer"` already exists):
```typescript
export interface OrgSummary {
  id: string;
  name: string;
  slug: string;
  role: AdminUserRole | null; // caller's role in this org; null for an act-as-only super-admin
}

// REPLACES the current `Me { email; role }`.
export interface Me {
  email: string;
  is_super_admin: boolean;
  acting_as: boolean;
  active_org: OrgSummary | null;
  orgs: OrgSummary[];
}

export interface SwitchOrgRequest { organization_id: string; }

export interface Member { email: string; role: AdminUserRole; added_by: string | null; }
export interface MemberCreate { email: string; role: AdminUserRole; }
export interface MemberRoleUpdate { role: AdminUserRole; }

export interface Organization { id: string; name: string; slug: string; status: string; }
export interface OrgCreate { name: string; slug: string; first_admin_email?: string | null; }
```
Note there is **no** org rename/delete/detail endpoint and **no** `super_admin` per-org role — don't add hooks for them.

---

## Critical integration notes (read before coding)

1. **`Me` shape change ripples through auth.** The only `me.role` readers are `useIsAdmin()` (`src/auth/useSession.ts`) and the `NavSidebar` footer. Update both in Task UI-1 so `npm run typecheck` stays green.
   - `useIsAdmin()` becomes: `const { data } = useSession(); return !!data && (data.is_super_admin || data.active_org?.role === "admin");` — a super-admin (incl. acting-as) counts as admin. (During act-as, `active_org.role` is `null` but `is_super_admin` is `true`.)
2. **`switch-org` invalidates everything.** On success the active org changed, so every cached query is stale. The mutation's `onSuccess` must `qc.invalidateQueries()` (all) — do NOT `setQueryData(["me"], resp)` (the response's `orgs` is `[]`); let `/me` refetch the full list.
3. **Replace, don't duplicate.** `features/adminUsers/` (`AdminUsersPage` + hooks + the `admin-users` route + the "Admin Users" nav item) calls the deleted `/v1/admin/admin-users`. Convert it to the members feature (Task UI-3): new `features/members/`, repoint to `/v1/admin/members`, rename the route to `members` + nav label to "Members", and delete `features/adminUsers/` + the `AdminUser`/`AdminUserCreate` types.
4. **Follow existing files as templates** (read them — don't reinvent): `features/contacts/hooks.ts` (query/mutation + invalidation), `features/contacts/ContactsPage.tsx` (page + inline mutate), `features/adminUsers/AdminUsersPage.tsx` (add/remove form + admin gate), `src/test/ContactsPage.test.tsx` (test harness), `components/NavSidebar.tsx` (nav + footer), `components/AppLayout.tsx` (shell).

---

## Task UI-1: types + session/auth wiring + org hooks

**Files:** `src/types/api.ts` (types above), `src/auth/useSession.ts` (useIsAdmin logic), `src/components/NavSidebar.tsx` (footer uses `me.active_org`), new `src/features/orgs/hooks.ts`. Tests: `src/test/useSession.test.tsx` (or extend existing).

- [ ] **Step 1 — failing test:** assert `useIsAdmin()` returns true for `{is_super_admin:true, active_org:null}` and for `{is_super_admin:false, active_org:{role:"admin"}}`, false for `{is_super_admin:false, active_org:{role:"viewer"}}`. Mock `lib/api` `get("/v1/auth/me")`.
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement:** replace the `Me` type + add the others; update `useIsAdmin()` per note 1; create `features/orgs/hooks.ts` with:
  - `useSwitchOrg()` — `useMutation<Me, ApiError, SwitchOrgRequest>({ mutationFn: b => api.post<Me>("/v1/auth/switch-org", b), onSuccess: () => void qc.invalidateQueries(), onError: e => pushToast(e.detail) })`.
  - `useOrganizations(enabled)` — `useQuery<Organization[]>({ queryKey: ["organizations"], queryFn: () => api.get("/v1/admin/organizations"), enabled })` (caller passes `me.is_super_admin`).
  - Update the `NavSidebar` footer to read `me.active_org?.name` + `me.active_org?.role` instead of `me.role` (keep email + logout). Keep it minimal here; the switcher UI is UI-2.
- [ ] **Step 4 — run tests + `npm run typecheck`, expect PASS** (typecheck is the real gate for the ripple). Commit.

## Task UI-2: org switcher + acting-as banner

**Files:** new `src/components/OrgSwitcher.tsx`, new `src/components/ActingAsBanner.tsx`; mount switcher in `NavSidebar` footer, banner in `AppLayout` above the `<Outlet/>`. Tests: `src/test/OrgSwitcher.test.tsx`, `src/test/ActingAsBanner.test.tsx`.

- [ ] **Step 1 — failing tests:** switcher renders `me.active_org.name` and the list of `me.orgs`; selecting another org calls `POST /v1/auth/switch-org {organization_id}`; for a super-admin it also offers orgs from `useOrganizations()` not in `me.orgs` (act-as). Banner: renders only when `me.acting_as`, shows `me.active_org.name`, and "Exit" switches to `me.orgs[0]` (super-admins always have ≥1 membership via bootstrap; if `me.orgs` is empty, the Exit control links to the org console route instead).
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** both components using `useSession`, `useSwitchOrg`, `useOrganizations` (the latter only enabled when `me.is_super_admin`). Mount per the shell map (switcher in the `NavSidebar` footer; banner at the top of `AppLayout`'s main column). Surface `switch-org` errors via the mutation's `onError` (already toasts).
- [ ] **Step 4 — run tests + typecheck, expect PASS.** Commit.

## Task UI-3: members page (replaces admin-users)

**Files:** new `src/features/members/hooks.ts` + `src/features/members/MembersPage.tsx`; `src/routes.tsx` (rename `admin-users` route → `members`, point to `MembersPage`); `src/components/NavSidebar.tsx` (rename nav item label/`to`); delete `src/features/adminUsers/`; remove `AdminUser`/`AdminUserCreate` from `types/api.ts`. Tests: `src/test/MembersPage.test.tsx` (mirror `ContactsPage.test.tsx`).

- [ ] **Step 1 — failing tests:** admin sees the member list (`GET /v1/admin/members`); can add (`POST`), change role (`PATCH /{email}`), remove (`DELETE /{email}`); a viewer sees "Admins only" (gate on `useIsAdmin()`); a 409 on removing the last admin surfaces as a toast.
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** `features/members/hooks.ts` (`const MEMBERS_KEY = ["members"] as const`; `useMembers`, `useAddMember`, `useSetMemberRole`, `useRemoveMember` — each invalidates `MEMBERS_KEY`, `onError: pushToast(e.detail)`), and `MembersPage.tsx` mirroring `AdminUsersPage.tsx` (add form: email + role select; table: email, role, added_by; remove via `ConfirmDialog`; role change via `<Select>`; writes gated on `useIsAdmin()`). Wire the route + nav rename; delete `features/adminUsers/`.
- [ ] **Step 4 — run tests + lint + typecheck, expect PASS** (confirm no dangling imports of the deleted feature/types). Commit.

## Task UI-4: super-admin org console

**Files:** new `src/features/orgs/OrgConsolePage.tsx` (reuse `features/orgs/hooks.ts` from UI-1; add `useCreateOrg`); `src/routes.tsx` (new `organizations` route under `PageLayout`); `src/components/NavSidebar.tsx` (new nav item, shown only when `me.is_super_admin` — add a `superAdminOnly?: boolean` to the `NavItem` interface + filter on `useSession().data?.is_super_admin`). Tests: `src/test/OrgConsolePage.test.tsx`.

- [ ] **Step 1 — failing tests:** a super-admin sees the org list (`GET /v1/admin/organizations`) and can create one (`POST` name+slug, optional first-admin email); a non-super-admin sees "Super-admins only"; a duplicate slug (409) surfaces as a toast; each row has an "Act as" button calling `useSwitchOrg({ organization_id })`.
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** `useCreateOrg()` (`useMutation<Organization, ApiError, OrgCreate>`, invalidates `["organizations"]`, `onError: pushToast`), `OrgConsolePage` (table of orgs + create form + per-row "Act as"), gate the whole page on `me.is_super_admin` (else "Super-admins only" like `AdminUsersPage` gates viewers), add the route + the `superAdminOnly` nav item.
- [ ] **Step 4 — run tests + lint + typecheck, expect PASS.** Commit.

---

## Final gate
`cd apps/admin-ui && npm test && npm run lint && npm run typecheck` — all clean. Then finish the branch → PR 2 stacked on `feat/tenancy-p2-identity-rbac`.

## Self-review
- Contract matches PR #100 exactly (endpoints/shapes verified against the shipped routers/schemas). No invented endpoints.
- The `Me` ripple is contained to `useIsAdmin` + `NavSidebar` footer (typecheck enforces completeness).
- AdminUsers→Members is a replacement (route/nav/types/feature dir all moved), not a duplicate.
- Act-as is treated as admin in the UI via `is_super_admin`, matching the backend's full-write act-as.
