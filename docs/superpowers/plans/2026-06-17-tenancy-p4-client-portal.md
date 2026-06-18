# P4 — Client Portal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Open the existing admin app to client-organization users by hiding operator-only surfaces (Profiles/Defaults/Variables → super-admin only), gating the audit log to ADMIN, and giving client ADMINs a read view of their own org — all enforced server-side and mirrored in the UI.

**Architecture:** Approach A — same `admin-ui` build, role-gated. Backend: swap the router-level auth dependency on operator-only routers from `require_admin_session` → `require_super_admin`, and gate `admin_audit` to `require_admin_role(ADMIN)`. Frontend: re-tag nav, add route guards (`RequireSuperAdmin`/`RequireAdmin`) and a role-aware index landing (`HomeLanding`). No schema migration — all P1–P3 plumbing (RLS, `get_tenant_db`, `AdminPrincipal`, memberships) already exists.

**Tech Stack:** FastAPI + SQLAlchemy 2.x async (Python 3.14, uv); Postgres RLS; React + react-router v6 + react-query v5 + Vitest/RTL (admin-ui).

**Spec:** `docs/superpowers/specs/2026-06-17-tenancy-p4-client-portal-design.md`

---

## Execution Ordering (read before starting)

Implement **in order**. Per-task TDD: write the failing test → run it red → implement → run it green → commit.

- **Unit A (backend gating)** is independent of the frontend and lands first. A1 writes the failing gating test; A2 makes the operator-endpoint assertions pass; A3 makes the audit assertions pass.
- **Unit B (frontend)** is runtime-independent of the backend (the admin-ui tests mock `/v1/auth/me`), so it can be done in parallel, but commit it after A so the contract is final.
- **Unit C** is the full green-gate run + the operator-gated live onboarding (manual).

Gate commands before each backend commit: `cd apps/api && uv run pytest -q && ruff check . && ruff format . && uv run mypy`. Before each admin-ui commit: `cd apps/admin-ui && npm test && npm run lint && npm run typecheck`. **CI runs mypy — do not skip it locally.**

---

## Unit A — Backend: hide operator surfaces, gate the audit log

### Task A1: Failing gating test

**Files:**
- Create: `apps/api/tests/test_p4_client_portal_gating.py`

This is the P4 auditor-facing isolation proof (mirrors `test_rls_p2_isolation.py`). It will go fully green only after A2 (operator routers) **and** A3 (audit) land — run it red now, partially green after A2, fully green after A3.

- [ ] **Step 1: Write the test file**

```python
"""P4: client-portal gating + operator-surface isolation.

Proves the P4 admin-plane guarantees:
- Operator-only routers (profiles, defaults, custom-variables, the profile-authoring
  catalogs) require a super-admin (USAN operator): a client-org ADMIN gets 403, a
  super-admin (acting-as) gets 200.
- The audit log is ADMIN-gated: a client VIEWER gets 403, a client ADMIN gets 200
  (org-scoped by the same RLS seam proven in test_rls_p2_isolation).

Helpers mirror test_rls_p2_isolation (superuser-engine seeding + session cookies).
The 403 assertions hit the router-level gate before get_tenant_db, so the shared
`client` fixture suffices; the 200 assertions scope the shared get_tenant_db override
to the principal's org via conftest's `act_as_org`.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import act_as_org
from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings

# GET endpoints that become operator-only (super-admin) in P4.
OPERATOR_GET_ENDPOINTS = [
    "/v1/admin/profiles",
    "/v1/admin/defaults",
    "/v1/admin/custom-variables",
    "/v1/admin/voice-catalog",
    "/v1/admin/model-catalog",
    "/v1/admin/tool-catalog",
    "/v1/admin/variable-catalog",
]


def _super_url(app_async_database_url: str) -> str:
    """The superuser (RLS-bypassing) async DSN, derived from the usan_app one."""
    return app_async_database_url.replace("usan_app:usan_app@", "usan:usan@", 1)


async def _seed_member(super_async_url: str, email: str, org_id: uuid.UUID, role: str) -> None:
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, status, added_by) "
                    "VALUES (:e, 'active', 'test') ON CONFLICT (email) DO NOTHING"
                ),
                {"e": email.lower()},
            )
            await conn.execute(
                text(
                    "INSERT INTO memberships (email, organization_id, role, added_by) "
                    "VALUES (:e, :o, CAST(:r AS admin_role), 'test') "
                    "ON CONFLICT (email, organization_id) DO UPDATE SET role = EXCLUDED.role"
                ),
                {"e": email.lower(), "o": org_id, "r": role},
            )
    finally:
        await engine.dispose()


async def _seed_super_admin(super_async_url: str, email: str) -> None:
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, is_super_admin, status, added_by) "
                    "VALUES (:e, true, 'active', 'test') "
                    "ON CONFLICT (email) DO UPDATE SET is_super_admin = true"
                ),
                {"e": email.lower()},
            )
    finally:
        await engine.dispose()


def _member_cookie(email: str, org_id: uuid.UUID, role: AdminRole) -> dict[str, str]:
    token = issue_session(
        email, active_org_id=org_id, role=role, is_super_admin=False,
        acting_as=False, settings=get_settings(),
    )
    return {SESSION_COOKIE_NAME: token}


def _act_as_cookie(email: str, org_id: uuid.UUID) -> dict[str, str]:
    """A super-admin session acting-as ``org_id`` (no membership there)."""
    token = issue_session(
        email, active_org_id=org_id, role=AdminRole.ADMIN, is_super_admin=True,
        acting_as=True, settings=get_settings(),
    )
    return {SESSION_COOKIE_NAME: token}


@pytest.mark.parametrize("path", OPERATOR_GET_ENDPOINTS)
def test_client_admin_forbidden_on_operator_endpoints(
    client, two_orgs, app_async_database_url, path
):
    super_url = _super_url(app_async_database_url)
    _, org_b = two_orgs
    asyncio.run(_seed_member(super_url, "client@example.com", org_b, "admin"))
    # 403 fires at the router-level require_super_admin gate, before get_tenant_db.
    r = client.get(path, cookies=_member_cookie("client@example.com", org_b, AdminRole.ADMIN))
    assert r.status_code == 403, f"{path}: {r.status_code} {r.text}"


@pytest.mark.parametrize("path", OPERATOR_GET_ENDPOINTS)
def test_super_admin_allowed_on_operator_endpoints(
    client, two_orgs, app_async_database_url, path
):
    super_url = _super_url(app_async_database_url)
    org_a, _ = two_orgs
    asyncio.run(_seed_super_admin(super_url, "staff@usan.com"))
    # Scope the shared get_tenant_db override to the act-as target org.
    act_as_org(client.app, org_a)
    r = client.get(path, cookies=_act_as_cookie("staff@usan.com", org_a))
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"


def test_client_viewer_forbidden_on_audit(client, two_orgs, app_async_database_url):
    super_url = _super_url(app_async_database_url)
    org_a, _ = two_orgs
    asyncio.run(_seed_member(super_url, "viewer@example.com", org_a, "viewer"))
    act_as_org(client.app, org_a)
    r = client.get(
        "/v1/admin/audit",
        cookies=_member_cookie("viewer@example.com", org_a, AdminRole.VIEWER),
    )
    assert r.status_code == 403, r.text


def test_client_admin_allowed_on_own_audit(client, two_orgs, app_async_database_url):
    super_url = _super_url(app_async_database_url)
    org_a, _ = two_orgs
    asyncio.run(_seed_member(super_url, "auditor@example.com", org_a, "admin"))
    act_as_org(client.app, org_a)
    r = client.get(
        "/v1/admin/audit",
        cookies=_member_cookie("auditor@example.com", org_a, AdminRole.ADMIN),
    )
    assert r.status_code == 200, r.text
```

- [ ] **Step 2: Run it red**

Run: `cd apps/api && uv run pytest tests/test_p4_client_portal_gating.py -q`
Expected: FAILs — operator endpoints currently return 200 for a client admin (not yet gated), and `/v1/admin/audit` currently returns 200 for a viewer.

- [ ] **Step 3: Commit the failing test**

```bash
git add apps/api/tests/test_p4_client_portal_gating.py
git commit -m "test(api): P4 client-portal gating suite (red)"
```

---

### Task A2: Gate operator-only routers to `require_super_admin`

**Files:**
- Modify: `apps/api/src/usan_api/routers/admin_profiles.py:8,40`
- Modify: `apps/api/src/usan_api/routers/admin_defaults.py:6,15`
- Modify: `apps/api/src/usan_api/routers/admin_custom_variables.py:20,36`
- Modify: `apps/api/src/usan_api/routers/admin_profile_tests.py:31,51`
- Modify: `apps/api/src/usan_api/routers/admin_voice_catalog.py:26,38`
- Modify: `apps/api/src/usan_api/routers/admin_model_catalog.py:10,16`
- Modify: `apps/api/src/usan_api/routers/admin_tool_catalog.py:3,9`
- Modify: `apps/api/src/usan_api/routers/admin_variable_catalog.py:6,13`

Each change is the same two-line shape: (1) in the `from usan_api.auth import ...` line, replace `require_admin_session` with `require_super_admin`; (2) in `router = APIRouter(...)`, replace `dependencies=[Depends(require_admin_session)]` with `dependencies=[Depends(require_super_admin)]`. Per-route `require_admin_role(AdminRole.ADMIN)` gates (where present) stay — they are now redundant but harmless (a super-admin acting-as has role ADMIN).

- [ ] **Step 1: `admin_profiles.py`**

Line 8 — change:
```python
from usan_api.auth import get_tenant_db, require_admin_role, require_admin_session
```
to:
```python
from usan_api.auth import get_tenant_db, require_admin_role, require_super_admin
```
Line 40 — change `dependencies=[Depends(require_admin_session)],` to `dependencies=[Depends(require_super_admin)],`.

- [ ] **Step 2: `admin_defaults.py`**

Line 6 — change:
```python
from usan_api.auth import get_tenant_db, require_admin_session
```
to:
```python
from usan_api.auth import get_tenant_db, require_super_admin
```
Line 15 — change `dependencies=[Depends(require_admin_session)],` to `dependencies=[Depends(require_super_admin)],`.

- [ ] **Step 3: `admin_custom_variables.py`**

Line 20 — change:
```python
from usan_api.auth import get_tenant_db, require_admin_role, require_admin_session
```
to:
```python
from usan_api.auth import get_tenant_db, require_admin_role, require_super_admin
```
Line 36 — change `dependencies=[Depends(require_admin_session)],` to `dependencies=[Depends(require_super_admin)],`.

- [ ] **Step 4: `admin_profile_tests.py`**

Line 31 — change:
```python
from usan_api.auth import get_tenant_db, require_admin_role, require_admin_session
```
to:
```python
from usan_api.auth import get_tenant_db, require_admin_role, require_super_admin
```
Line 51 — change `dependencies=[Depends(require_admin_session)],` to `dependencies=[Depends(require_super_admin)],`.

- [ ] **Step 5: `admin_voice_catalog.py`**

Line 26 — change `from usan_api.auth import require_admin_session` to `from usan_api.auth import require_super_admin`.
Line 38 — change `dependencies=[Depends(require_admin_session)],` to `dependencies=[Depends(require_super_admin)],`.

- [ ] **Step 6: `admin_model_catalog.py`**

Line 10 — change `from usan_api.auth import require_admin_session` to `from usan_api.auth import require_super_admin`.
Line 16 — change `dependencies=[Depends(require_admin_session)],` to `dependencies=[Depends(require_super_admin)],`.

- [ ] **Step 7: `admin_tool_catalog.py`**

Line 3 — change `from usan_api.auth import require_admin_session` to `from usan_api.auth import require_super_admin`.
Line 9 — change `dependencies=[Depends(require_admin_session)],` to `dependencies=[Depends(require_super_admin)],`.

- [ ] **Step 8: `admin_variable_catalog.py`**

Line 6 — change `from usan_api.auth import get_tenant_db, require_admin_session` to `from usan_api.auth import get_tenant_db, require_super_admin`.
Line 13 — change `dependencies=[Depends(require_admin_session)],` to `dependencies=[Depends(require_super_admin)],`.

- [ ] **Step 9: Run the operator-endpoint assertions green**

Run: `cd apps/api && uv run pytest tests/test_p4_client_portal_gating.py -q -k operator_endpoints`
Expected: PASS (both the `client_admin_forbidden` and `super_admin_allowed` parametrized cases). The two audit tests still fail until A3.

- [ ] **Step 10: Guard against regressions in the existing per-router suites**

Run: `cd apps/api && uv run pytest tests/test_admin_profiles_api.py tests/test_admin_defaults_api.py tests/test_admin_custom_variables_api.py -q`
Expected: review any failures — pre-P4 tests that logged in as a plain ADMIN/VIEWER and expected 200 on these now-operator-only routers must be updated to use a super-admin session (mirror `_act_as_cookie` / `_seed_super_admin`). Fix them in this step so the targeted suites pass. (Exact files depend on what exists; if a suite is already super-admin-based or absent, note it and move on.)

- [ ] **Step 11: Lint + types + commit**

```bash
cd apps/api && ruff check . && ruff format . && uv run mypy
git add apps/api/src/usan_api/routers/admin_profiles.py apps/api/src/usan_api/routers/admin_defaults.py apps/api/src/usan_api/routers/admin_custom_variables.py apps/api/src/usan_api/routers/admin_profile_tests.py apps/api/src/usan_api/routers/admin_voice_catalog.py apps/api/src/usan_api/routers/admin_model_catalog.py apps/api/src/usan_api/routers/admin_tool_catalog.py apps/api/src/usan_api/routers/admin_variable_catalog.py apps/api/tests
git commit -m "feat(api): P4 — gate operator-only admin routers to super-admin"
```

---

### Task A3: Gate the audit log to ADMIN

**Files:**
- Modify: `apps/api/src/usan_api/routers/admin_audit.py:4,8-12`

The audit list is currently all-roles (`require_admin_session`). P4 makes it ADMIN-only so a client VIEWER can't read it, while a client ADMIN reads its own org's rows (RLS via `get_tenant_db`, unchanged). Gate at the router level so a viewer is rejected before `get_tenant_db` runs.

- [ ] **Step 1: Swap the import**

Line 4 — change:
```python
from usan_api.auth import get_tenant_db, require_admin_session
```
to:
```python
from usan_api.auth import get_tenant_db, require_admin_role
from usan_api.db.base import AdminRole
```

- [ ] **Step 2: Gate the router**

Lines 8-12 — change the router declaration:
```python
router = APIRouter(
    prefix="/v1/admin/audit",
    tags=["admin-audit"],
    dependencies=[Depends(require_admin_session)],
)
```
to:
```python
router = APIRouter(
    prefix="/v1/admin/audit",
    tags=["admin-audit"],
    # ADMIN-only (P4): a client VIEWER cannot read the audit log; a client ADMIN
    # reads only its own org's rows (RLS via get_tenant_db). require_admin_role
    # depends on require_admin_session, so unauthenticated still 401s.
    dependencies=[Depends(require_admin_role(AdminRole.ADMIN))],
)
```

- [ ] **Step 3: Run the P4 gating suite fully green**

Run: `cd apps/api && uv run pytest tests/test_p4_client_portal_gating.py -q`
Expected: PASS (all parametrized operator cases + both audit cases).

- [ ] **Step 4: Reconcile existing audit tests**

Run: `cd apps/api && uv run pytest tests/test_admin_audit_api.py -q` (if present)
Expected: any test that read `/v1/admin/audit` as a VIEWER and expected 200 must change to ADMIN (or assert 403). Fix in this step.

- [ ] **Step 5: Full backend suite + lint + types**

Run: `cd apps/api && uv run pytest -q && ruff check . && ruff format . && uv run mypy`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/routers/admin_audit.py apps/api/tests
git commit -m "feat(api): P4 — gate audit log to ADMIN (client own-org read)"
```

---

## Unit B — Frontend: guards, landing, nav re-tag

### Task B1: Route guards + role-aware landing

**Files:**
- Create: `apps/admin-ui/src/auth/RequireTier.tsx`
- Create: `apps/admin-ui/src/components/HomeLanding.tsx`
- Test: `apps/admin-ui/src/test/clientPortalGuards.test.tsx`

The guards and landing read the session directly (`useSession`) and return `null` while it loads, so they never redirect prematurely (the whole tree is already under `RequireAuth`, which shows the splash; the `null` is belt-and-suspenders that also keeps the unit tests honest). Non-super users hitting an operator route, and non-admins hitting an admin route, are redirected to `/calls`.

- [ ] **Step 1: Write the failing test**

```tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";

const getMock = vi.fn();
vi.mock("../lib/api", () => ({ api: { get: (u: string) => getMock(u) } }));
// Keep ProfilesListPage's data-fetching out of the HomeLanding test.
vi.mock("../features/profiles/ProfilesListPage", () => ({
  ProfilesListPage: () => <div>profiles-home</div>,
}));

import { HomeLanding } from "../components/HomeLanding";
import { RequireAdmin, RequireSuperAdmin } from "../auth/RequireTier";
import { meFixture } from "./meFixture";
import type { Me } from "../types/api";

let me: Me = meFixture("admin");

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") return Promise.resolve(me);
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function renderAt(ui: ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={ui} />
          <Route path="/calls" element={<div>calls-page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  getMock.mockImplementation(routeGet);
  me = meFixture("admin");
});

describe("HomeLanding", () => {
  it("redirects a client admin to /calls", async () => {
    me = meFixture("admin");
    renderAt(<HomeLanding />);
    expect(await screen.findByText("calls-page")).toBeInTheDocument();
  });
  it("renders Profiles for a super-admin", async () => {
    me = meFixture("admin", { is_super_admin: true });
    renderAt(<HomeLanding />);
    expect(await screen.findByText("profiles-home")).toBeInTheDocument();
  });
});

describe("RequireSuperAdmin", () => {
  it("redirects a non-super user to /calls", async () => {
    me = meFixture("admin");
    renderAt(
      <RequireSuperAdmin>
        <div>operator-only</div>
      </RequireSuperAdmin>,
    );
    expect(await screen.findByText("calls-page")).toBeInTheDocument();
  });
  it("renders children for a super-admin", async () => {
    me = meFixture("admin", { is_super_admin: true });
    renderAt(
      <RequireSuperAdmin>
        <div>operator-only</div>
      </RequireSuperAdmin>,
    );
    expect(await screen.findByText("operator-only")).toBeInTheDocument();
  });
});

describe("RequireAdmin", () => {
  it("redirects a viewer to /calls", async () => {
    me = meFixture("viewer");
    renderAt(
      <RequireAdmin>
        <div>admin-area</div>
      </RequireAdmin>,
    );
    expect(await screen.findByText("calls-page")).toBeInTheDocument();
  });
  it("renders children for a client admin", async () => {
    me = meFixture("admin");
    renderAt(
      <RequireAdmin>
        <div>admin-area</div>
      </RequireAdmin>,
    );
    expect(await screen.findByText("admin-area")).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run it red**

Run: `cd apps/admin-ui && npx vitest run src/test/clientPortalGuards.test.tsx`
Expected: FAIL (`../auth/RequireTier` and `../components/HomeLanding` do not exist).

- [ ] **Step 3: Implement `RequireTier.tsx`**

```tsx
import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { useSession } from "./useSession";

// Operator-only routes (Profiles, Defaults, Variables, Org console). A non-super
// user who deep-links here is sent to /calls — mirrors the backend 403 so a hand-
// typed URL never renders an operator page shell. Renders nothing while the session
// loads (RequireAuth already shows the splash above us) to avoid a premature redirect.
export function RequireSuperAdmin({ children }: { children: ReactNode }) {
  const { data, isLoading } = useSession();
  if (isLoading) return null;
  if (!data?.is_super_admin) return <Navigate to="/calls" replace />;
  return <>{children}</>;
}

// Client-ADMIN routes (Contacts, Members, Audit). A super-admin counts as admin
// everywhere (incl. acting-as, where active_org.role is null). A viewer is sent to
// /calls.
export function RequireAdmin({ children }: { children: ReactNode }) {
  const { data, isLoading } = useSession();
  if (isLoading) return null;
  const isAdmin = !!data && (data.is_super_admin || data.active_org?.role === "admin");
  if (!isAdmin) return <Navigate to="/calls" replace />;
  return <>{children}</>;
}
```

- [ ] **Step 4: Implement `HomeLanding.tsx`**

```tsx
import { Navigate } from "react-router-dom";
import { useSession } from "../auth/useSession";
import { ProfilesListPage } from "../features/profiles/ProfilesListPage";

// The index route ("/"). Operators land on Profiles (unchanged); client-org users
// cannot see Profiles (operator-only in P4), so they are sent to their call history.
export function HomeLanding() {
  const { data, isLoading } = useSession();
  if (isLoading) return null;
  if (!data?.is_super_admin) return <Navigate to="/calls" replace />;
  return <ProfilesListPage />;
}
```

- [ ] **Step 5: Run it green**

Run: `cd apps/admin-ui && npx vitest run src/test/clientPortalGuards.test.tsx`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add apps/admin-ui/src/auth/RequireTier.tsx apps/admin-ui/src/components/HomeLanding.tsx apps/admin-ui/src/test/clientPortalGuards.test.tsx
git commit -m "feat(admin-ui): P4 — tier route guards + role-aware home landing"
```

---

### Task B2: Wire the guards + landing into the router

**Files:**
- Modify: `apps/admin-ui/src/routes.tsx`

- [ ] **Step 1: Add imports**

After line 17 (`import { AcceptInvitePage } from "./features/invites/AcceptInvitePage";`), add:
```tsx
import { HomeLanding } from "./components/HomeLanding";
import { RequireAdmin, RequireSuperAdmin } from "./auth/RequireTier";
```

- [ ] **Step 2: Replace the children block**

Replace the `children` array (lines 33-51, the `element: <PageLayout />` children plus the editor route) with:
```tsx
    children: [
      {
        element: <PageLayout />,
        children: [
          { index: true, element: <HomeLanding /> },
          {
            path: "profiles/:id/versions",
            element: (
              <RequireSuperAdmin>
                <VersionHistoryPage />
              </RequireSuperAdmin>
            ),
          },
          { path: "calls", element: <CallsPage /> },
          { path: "calls/:id", element: <CallDetailPage /> },
          { path: "queues", element: <QueuesPage /> },
          {
            path: "contacts",
            element: (
              <RequireAdmin>
                <ContactsPage />
              </RequireAdmin>
            ),
          },
          {
            path: "defaults",
            element: (
              <RequireSuperAdmin>
                <DefaultsPage />
              </RequireSuperAdmin>
            ),
          },
          {
            path: "custom-variables",
            element: (
              <RequireSuperAdmin>
                <CustomVariablesPage />
              </RequireSuperAdmin>
            ),
          },
          {
            path: "audit",
            element: (
              <RequireAdmin>
                <AuditPage />
              </RequireAdmin>
            ),
          },
          {
            path: "members",
            element: (
              <RequireAdmin>
                <MembersPage />
              </RequireAdmin>
            ),
          },
          {
            path: "organizations",
            element: (
              <RequireSuperAdmin>
                <OrgConsolePage />
              </RequireSuperAdmin>
            ),
          },
        ],
      },
      {
        path: "profiles/:id",
        element: (
          <RequireSuperAdmin>
            <ProfileEditorPage />
          </RequireSuperAdmin>
        ),
      },
    ],
```

- [ ] **Step 3: Typecheck + build**

Run: `cd apps/admin-ui && npm run typecheck && npm run build`
Expected: both succeed (no unused imports, no type errors).

- [ ] **Step 4: Commit**

```bash
git add apps/admin-ui/src/routes.tsx
git commit -m "feat(admin-ui): P4 — guard operator/admin routes + role-aware index"
```

---

### Task B3: Re-tag the nav + fix the nav tests

**Files:**
- Modify: `apps/admin-ui/src/components/NavSidebar.tsx:33-64`
- Modify: `apps/admin-ui/src/test/NavSidebar.test.tsx`

- [ ] **Step 1: Update the failing nav tests first**

In `apps/admin-ui/src/test/NavSidebar.test.tsx`, extend the harness to vary super-admin, and fix the assertions that the new gating changes.

Add `cleanup` to the testing-library import at line 3:
```tsx
import { cleanup, render, screen } from "@testing-library/react";
```

Replace the harness block (lines 21-47, from `let role` through the `beforeEach`) with:
```tsx
let role: "admin" | "viewer" = "viewer";
let superAdmin = false;

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") {
    return Promise.resolve(meFixture(role, superAdmin ? { is_super_admin: true } : {}));
  }
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function renderSidebar() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <NavSidebar />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  getMock.mockImplementation(routeGet);
  role = "viewer";
  superAdmin = false;
});
```

Replace the test "shows Variables under Config for a viewer (not adminOnly)" (lines 81-89) with:
```tsx
  it("hides Variables/Defaults/Profiles for a non-super user (operator-only in P4)", async () => {
    role = "admin"; // a client ADMIN is still not an operator
    renderSidebar();
    await screen.findByText("me@example.com");
    expect(screen.queryByRole("link", { name: "Variables" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Defaults" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Profiles" })).not.toBeInTheDocument();
  });

  it("shows Profiles/Defaults/Variables for a super-admin operator", async () => {
    role = "admin";
    superAdmin = true;
    renderSidebar();
    await screen.findByText("me@example.com");
    expect(screen.getByRole("link", { name: "Profiles" })).toHaveAttribute("href", "/");
    expect(screen.getByRole("link", { name: "Defaults" })).toHaveAttribute("href", "/defaults");
    expect(screen.getByRole("link", { name: "Variables" })).toHaveAttribute(
      "href",
      "/custom-variables",
    );
  });

  it("hides Audit for a viewer, shows it for a client admin (P4 ADMIN-gated)", async () => {
    role = "viewer";
    renderSidebar();
    await screen.findByText("me@example.com");
    expect(screen.queryByRole("link", { name: "Audit" })).not.toBeInTheDocument();
    cleanup();
    role = "admin";
    renderSidebar();
    await screen.findByText("me@example.com");
    expect(screen.getByRole("link", { name: "Audit" })).toHaveAttribute("href", "/audit");
  });
```

Replace the test "groups render in order Build, Config, Operate, System" (lines 108-123) with a super-admin (the only tier that sees all four groups):
```tsx
  it("groups render in order Build, Config, Operate, System for an operator", async () => {
    role = "admin";
    superAdmin = true;
    renderSidebar();
    await screen.findByText("me@example.com");
    const expectFollows = (first: HTMLElement, second: HTMLElement) => {
      expect(
        first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    };
    const build = screen.getByText("Build");
    const config = screen.getByText("Config");
    const operate = screen.getByText("Operate");
    const system = screen.getByText("System");
    expectFollows(build, config);
    expectFollows(config, operate);
    expectFollows(operate, system);
  });
```

(The kept tests — "viewer sees Operate", "Contacts hidden for viewer", "Contacts visible for admin", "renders a decorative icon" with `role="admin"` — remain valid: a client admin still sees Contacts/Calls/Queues/Members/Audit.)

- [ ] **Step 2: Run the nav tests red**

Run: `cd apps/admin-ui && npx vitest run src/test/NavSidebar.test.tsx`
Expected: FAIL — the new assertions expect Profiles/Defaults/Variables to be `superAdminOnly` and Audit to be `adminOnly`, which the nav config does not yet do.

- [ ] **Step 3: Re-tag the nav config**

In `apps/admin-ui/src/components/NavSidebar.tsx`, replace the `GROUPS` constant (lines 33-64) with:
```tsx
const GROUPS: NavGroup[] = [
  {
    heading: "Build",
    items: [{ to: "/", label: "Profiles", icon: ProfilesIcon, superAdminOnly: true }],
  },
  {
    heading: "Config",
    items: [
      { to: "/contacts", label: "Contacts", icon: ContactsIcon, adminOnly: true },
      // Operator-only in P4 (profile authoring is not exposed to client orgs).
      { to: "/defaults", label: "Defaults", icon: DefaultsIcon, superAdminOnly: true },
      { to: "/custom-variables", label: "Variables", icon: VariablesIcon, superAdminOnly: true },
    ],
  },
  {
    heading: "Operate",
    items: [
      { to: "/calls", label: "Calls", icon: CallsIcon },
      { to: "/queues", label: "Queues", icon: QueuesIcon },
    ],
  },
  {
    heading: "System",
    items: [
      // ADMIN-gated in P4: a client ADMIN reads its own org's audit; viewers do not.
      { to: "/audit", label: "Audit", icon: AuditIcon, adminOnly: true },
      { to: "/members", label: "Members", icon: MembersIcon, adminOnly: true },
      {
        to: "/organizations",
        label: "Organizations",
        icon: OrganizationsIcon,
        superAdminOnly: true,
      },
    ],
  },
];
```

- [ ] **Step 4: Run the nav tests green**

Run: `cd apps/admin-ui && npx vitest run src/test/NavSidebar.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/admin-ui/src/components/NavSidebar.tsx apps/admin-ui/src/test/NavSidebar.test.tsx
git commit -m "feat(admin-ui): P4 — re-tag nav (operator-only Profiles/Defaults/Variables, ADMIN audit)"
```

---

## Unit C — Verification & onboarding

### Task C1: Full green gate

- [ ] **Step 1: Backend**

Run: `cd apps/api && uv run pytest -q && ruff check . && ruff format --check . && uv run mypy`
Expected: all pass. (`test_p4_client_portal_gating.py` green; no regressions in the existing admin suites.)

- [ ] **Step 2: Admin UI**

Run: `cd apps/admin-ui && npm test && npm run lint && npm run typecheck && npm run build`
Expected: all pass. Note: full `npx vitest run` can flake 1-3 tests with 5000ms timeouts under parallel CPU load — re-run/isolate a suspected failure before treating it as a regression.

- [ ] **Step 3: Commit any lint/format fixups**

```bash
git add -A
git commit -m "chore: P4 — formatting/lint fixups" || echo "nothing to commit"
```

---

### Task C2: Live onboarding of a 2nd org (operator-gated, manual)

Not automated — requires a running stack and a browser. This is the live proof of the surface matrix (spec §4) and isolation (spec §5.3, §8).

- [ ] **Step 1: Bring up the stack and sign in as a super-admin (USAN operator).**

Run: `make up` (see CLAUDE.md), then open the admin UI and sign in with a Google account that is a seeded super-admin.

- [ ] **Step 2: Create a second org + invite its first ADMIN.** Use the Organizations console (`/organizations`) to create org "Acme Care" and add/invite its first ADMIN (P2/P3 flows). Copy the invite link.

- [ ] **Step 3: Accept as the client ADMIN** (a different Google identity matching the invited email) and verify the surface matrix:
  - Visible: Calls, Queues, Contacts, Audit, Members (with Invites). Landing is `/calls`.
  - Absent: Build/Profiles, Defaults, Variables, Organizations. Deep-linking `/`, `/profiles/<id>`, `/defaults`, `/custom-variables`, `/organizations` redirects to `/calls`; the API returns 403 for those endpoints.
  - The Audit page shows only Acme Care's rows (e.g. the "org created / first member added" entries, attributed to the USAN super-admin).

- [ ] **Step 4: Sign in as a client VIEWER** (invite a second Acme member as VIEWER) and verify read-only: Calls + Queues visible; Audit, Members, Contacts absent / redirect to `/calls`.

- [ ] **Step 5: Confirm two-way isolation.** As the USAN operator, confirm USAN's data is unchanged and Acme's data never appears in USAN's views (and vice versa). Note: a freshly-onboarded org has no call data yet (calling stays single-org in P4) — confirm the empty pages render a friendly empty state rather than an error; if any page renders a broken/empty shell, add an empty-state message in a follow-up commit (`feat(admin-ui): P4 — empty states for a new client org`).

- [ ] **Step 6: Record the result.** Append a short "P4 live validation" note (date + what was verified) to the plan or the PR description.

---

## Self-Review (completed during authoring)

- **Spec coverage:** §4 matrix → A2 (operator routers), A3 (audit), B3 (nav), B2 (route guards), B1 (landing). §5 backend gates → A2/A3 + the A1 isolation suite. §6 frontend → B1/B2/B3; §6.4 "org name in header" is already present (`NavSidebar.tsx:144-154`), so no task; empty-state polish → C2 step 5. §7 audit read surface → A3 + C2 step 3. §8 onboarding → C2. §3 tiers → guards use `is_super_admin` / `active_org.role`.
- **Placeholder scan:** every code step shows complete code; the only judgment steps (A2 step 10, A3 step 4) are "reconcile pre-existing tests," which depend on what currently exists and are explicitly bounded.
- **Type consistency:** `require_super_admin`, `require_admin_role(AdminRole.ADMIN)`, `get_tenant_db`, `AdminRole` match `auth.py`/`db/base.py`; `RequireSuperAdmin`/`RequireAdmin`/`HomeLanding`, `meFixture(role, over)`, and the `Me` fields (`is_super_admin`, `active_org.role`) match the admin-ui source.
- **Non-goals:** no migration, no per-org calling, no client profile authoring, no new contact-write endpoints — consistent with spec §2.
