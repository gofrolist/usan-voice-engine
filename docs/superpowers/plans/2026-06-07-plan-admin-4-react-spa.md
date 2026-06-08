# Admin UI P4 — React SPA Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A self-hosted React admin console (`apps/admin-ui`) for operators to manage agent profiles end to end — list/create/clone, edit every config bundle, publish with a draft-vs-live diff, browse version history and roll back, assign profiles to elders, set per-direction defaults, view the audit log, and manage the SSO allow-list — talking only to the existing `apps/api` over the Google-SSO session cookie.

**Architecture:** Vite + React + TypeScript SPA, same-origin with the API (Caddy proxies `/v1/*` → `api`; Vite dev-proxies in development). No token handling — the browser holds the HttpOnly session cookie; a `401` redirects to `/v1/auth/login`. Server state via TanStack Query; routing via React Router; forms via react-hook-form + Zod (client validation mirroring the server Pydantic rules); UI via Tailwind + a small shadcn-style component set; long prompt fields via Monaco (with a `<textarea>` fallback). Three small API endpoints are added first so the elders/audit screens have a backend.

**Tech Stack:** Vite 6, React 19, TypeScript 5, @tanstack/react-query 5, react-router-dom 7, react-hook-form 7 + zod 3, tailwindcss 3, @monaco-editor/react, vitest + @testing-library/react + jsdom. Node 22 / npm.

**Scope:** `api` (3 additive endpoints, no migration) + new `admin-ui`. **Out of scope (P5):** the static-serving container, Caddy site block + CIDR, Terraform DNS, the CI build job, secret seeding — P4 produces the app + `vite build` static output only. Playwright E2E is authored but runs in CI/P5 (no browser here).

**Branch:** `admin-ui-p4`, stacked on `admin-ui-p3`. PR base = `admin-ui-p3`.

---

## Design decisions

1. **Same-origin, cookie-only auth.** The SPA never sees the Google token or the session JWT. Every `fetch` uses `credentials: "include"` and relative `/v1/...` URLs. A `401` from any call triggers a full-page redirect to `/v1/auth/login` (the API then bounces to Google and back to `/`). `useSession()` calls `GET /v1/auth/me` once; while loading it shows a splash, on `401` it redirects, on success it exposes `{email, role}` and gates admin-only UI on `role === "admin"`.
2. **Server state only in TanStack Query.** No Redux. Query keys per resource; mutations invalidate the relevant keys. Optimistic-concurrency / 409s surface as toast errors with a "reload" affordance.
3. **Validation mirrors the server.** A single `agentConfigSchema` (Zod) reproduces `schemas/agent_config.py` (lengths, ranges, brace rejection, template-slot whitelist, endpointing order, tool-name set). The editor validates locally for instant feedback; the server remains the source of truth (422s map back onto fields).
4. **Publish shows a diff.** The publish dialog renders a field-level diff of `draft_config` vs the current live version (`GET /versions/{published_version}`), so an operator sees exactly what will change before confirming.
5. **Three API endpoints added (api scope).** The design's elders + audit screens need backends that P1–P3 didn't build (per design §6.2/§15 the elder-assignment setter was deferred). P4 adds them minimally: `GET /v1/admin/audit`, `GET /v1/admin/elders`, `PUT /v1/admin/elders/{id}/profile`. The Cartesia **voices** catalog proxy is *not* built — the voice field is a plain text input (design §12 degradation), keeping P4 free of an external dependency.
6. **Kept out of the Python apps.** `apps/admin-ui` has its own `package.json`/lint/build; nothing in `apps/api` or `services/agent` imports it. CI/serving wiring is P5.

---

# Part 1 — API additions (`apps/api`)

## Task A1: `GET /v1/admin/audit` (paged audit log)

**Files:**
- Create: `apps/api/src/usan_api/routers/admin_audit.py` (router; note the repo is `repositories/admin_audit.py`)
- Create: `apps/api/src/usan_api/schemas/admin.py` (`AuditEntryOut`, `ElderSummary`, `AssignProfileRequest`)
- Modify: `apps/api/src/usan_api/main.py` (include the router)
- Test: `apps/api/tests/test_admin_audit_api.py`

- [ ] **Step 1: Schema** — Create `apps/api/src/usan_api/schemas/admin.py`:

```python
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class AuditEntryOut(BaseModel):
    id: int
    actor_email: str
    action: str
    entity_type: str | None = None
    entity_id: str | None = None
    detail: dict[str, Any]
    created_at: datetime


class ElderSummary(BaseModel):
    id: uuid.UUID
    name: str
    masked_phone: str
    agent_profile_id: uuid.UUID | None = None
    agent_profile_name: str | None = None


class AssignProfileRequest(BaseModel):
    # null clears the assignment (fall back to the per-direction default).
    agent_profile_id: uuid.UUID | None = None
```

- [ ] **Step 2: Failing test** — Create `apps/api/tests/test_admin_audit_api.py`:

```python
def test_audit_requires_session(client):
    assert client.get("/v1/admin/audit").status_code == 401


def test_audit_lists_recent_entries(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": "p-audit"}).json()["id"]
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"})
    r = client.get("/v1/admin/audit")
    assert r.status_code == 200
    actions = {e["action"] for e in r.json()}
    assert "profile.publish" in actions
    assert all("actor_email" in e for e in r.json())


def test_audit_limit_is_clamped(client, admin_session):
    assert client.get("/v1/admin/audit?limit=100000").status_code == 200
```

- [ ] **Step 3: Router** — Create `apps/api/src/usan_api/routers/admin_audit.py`:

```python
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import require_admin_session
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit as repo
from usan_api.schemas.admin import AuditEntryOut

router = APIRouter(
    prefix="/v1/admin/audit",
    tags=["admin-audit"],
    dependencies=[Depends(require_admin_session)],
)


@router.get("", response_model=list[AuditEntryOut])
async def list_audit(
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[AuditEntryOut]:
    rows = await repo.list_recent(db, limit=limit)
    return [
        AuditEntryOut(
            id=r.id,
            actor_email=r.actor_email,
            action=r.action,
            entity_type=r.entity_type,
            entity_id=r.entity_id,
            detail=r.detail,
            created_at=r.created_at,
        )
        for r in rows
    ]
```

Register in `main.py`: add `admin_audit` to the routers import and `app.include_router(admin_audit.router)`.

- [ ] **Step 4: Run** — `cd apps/api && uv run pytest tests/test_admin_audit_api.py -v` → PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(api): GET /v1/admin/audit (paged audit log, session-guarded)"`

## Task A2: `GET /v1/admin/elders` + `PUT /v1/admin/elders/{id}/profile`

**Files:**
- Create: `apps/api/src/usan_api/routers/admin_elders.py`
- Modify: `apps/api/src/usan_api/repositories/elders.py` (add `list_with_profile`, `assign_profile`)
- Modify: `apps/api/src/usan_api/main.py` (include router)
- Test: `apps/api/tests/test_admin_elders_api.py`

- [ ] **Step 1: Repo methods** — append to `repositories/elders.py` (verify/extend its imports for `select`, `uuid`, `Elder`, `AgentProfile`):

```python
async def list_with_profile(db: AsyncSession) -> list[tuple[Elder, str | None]]:
    """Elders with their assigned profile name (None if unassigned). Ordered by name."""
    result = await db.execute(
        select(Elder, AgentProfile.name)
        .outerjoin(AgentProfile, Elder.agent_profile_id == AgentProfile.id)
        .order_by(Elder.name)
    )
    return [(row[0], row[1]) for row in result.all()]


async def assign_profile(
    db: AsyncSession, elder_id: uuid.UUID, profile_id: uuid.UUID | None
) -> Elder | None:
    """Set (or clear, with None) an elder's agent_profile_id. Caller commits."""
    elder = await db.get(Elder, elder_id)
    if elder is None:
        return None
    elder.agent_profile_id = profile_id
    await db.flush()
    return elder
```

- [ ] **Step 2: Failing test** — Create `apps/api/tests/test_admin_elders_api.py`:

```python
import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def _seed_elder(async_database_url: str, name: str, phone: str) -> str:
    eid = str(uuid.uuid4())
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO elders (id, name, phone_e164, timezone) "
                    "VALUES (CAST(:id AS uuid), :n, :p, 'America/New_York')"
                ),
                {"id": eid, "n": name, "p": phone},
            )
    finally:
        await engine.dispose()
    return eid


def test_elders_requires_session(client):
    assert client.get("/v1/admin/elders").status_code == 401


def test_list_and_assign_profile(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_elder(async_database_url, "Ada Lovelace", "+15551230001"))
    pid = client.post("/v1/admin/profiles", json={"name": "p-elder"}).json()["id"]

    listed = client.get("/v1/admin/elders").json()
    me = next(e for e in listed if e["id"] == eid)
    assert me["name"] == "Ada Lovelace"
    assert me["masked_phone"].endswith("0001")
    assert me["masked_phone"].startswith("***")
    assert me["agent_profile_id"] is None

    r = client.put(f"/v1/admin/elders/{eid}/profile", json={"agent_profile_id": pid})
    assert r.status_code == 200
    assert r.json()["agent_profile_id"] == pid
    assert r.json()["agent_profile_name"] == "p-elder"

    r2 = client.put(f"/v1/admin/elders/{eid}/profile", json={"agent_profile_id": None})
    assert r2.status_code == 200
    assert r2.json()["agent_profile_id"] is None


def test_assign_unknown_elder_404(client, admin_session):
    r = client.put(f"/v1/admin/elders/{uuid.uuid4()}/profile", json={"agent_profile_id": None})
    assert r.status_code == 404


def test_assign_unknown_profile_400(client, admin_session, async_database_url):
    eid = asyncio.run(_seed_elder(async_database_url, "Grace Hopper", "+15551230002"))
    r = client.put(
        f"/v1/admin/elders/{eid}/profile", json={"agent_profile_id": str(uuid.uuid4())}
    )
    assert r.status_code == 400
```

- [ ] **Step 3: Router** — Create `apps/api/src/usan_api/routers/admin_elders.py`:

```python
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import require_admin_role, require_admin_session
from usan_api.db.base import AdminRole
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.admin import AssignProfileRequest, ElderSummary

router = APIRouter(
    prefix="/v1/admin/elders",
    tags=["admin-elders"],
    dependencies=[Depends(require_admin_session)],
)


def _mask(phone: str) -> str:
    return "***" + phone[-4:] if phone else "unknown"


def _summary(elder, profile_name: str | None) -> ElderSummary:  # type: ignore[no-untyped-def]
    return ElderSummary(
        id=elder.id,
        name=elder.name,
        masked_phone=_mask(elder.phone_e164),
        agent_profile_id=elder.agent_profile_id,
        agent_profile_name=profile_name,
    )


@router.get("", response_model=list[ElderSummary])
async def list_elders(db: AsyncSession = Depends(get_db)) -> list[ElderSummary]:
    return [_summary(e, name) for e, name in await elders_repo.list_with_profile(db)]


@router.put("/{elder_id}/profile", response_model=ElderSummary)
async def assign_profile(
    elder_id: uuid.UUID,
    body: AssignProfileRequest,
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> ElderSummary:
    try:
        elder = await elders_repo.assign_profile(db, elder_id, body.agent_profile_id)
        if elder is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="elder not found")
        await admin_audit.record(
            db,
            actor_email=actor,
            action="elder.assign_profile",
            entity_type="elder",
            entity_id=str(elder_id),
            detail={
                "agent_profile_id": str(body.agent_profile_id) if body.agent_profile_id else None
            },
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail="unknown agent_profile_id") from exc
    profile_name = None
    if elder.agent_profile_id is not None:
        prof = await profiles_repo.get_profile(db, elder.agent_profile_id)
        profile_name = prof.name if prof else None
    return _summary(elder, profile_name)
```

Register in `main.py` (`admin_elders` import + `include_router`).

- [ ] **Step 4: Run** — `uv run pytest tests/test_admin_elders_api.py -v` → PASS.
- [ ] **Step 5: Lint/type/commit** — `ruff check . && ruff format . && uv run mypy`; commit `feat(api): admin elders list + profile assignment endpoints`.

---

# Part 2 — Frontend (`apps/admin-ui`)

## Frontend file structure

```
apps/admin-ui/
  package.json, tsconfig.json, tsconfig.node.json, vite.config.ts,
  tailwind.config.js, postcss.config.js, index.html, .eslintrc.cjs, .gitignore,
  vitest.config.ts, vitest.setup.ts
  src/
    main.tsx                # QueryClientProvider + RouterProvider
    index.css               # tailwind layers
    lib/{api.ts, queryClient.ts, cn.ts}
    types/api.ts            # TS mirrors of the Pydantic schemas
    config/{agentConfigSchema.ts, fieldMeta.ts}
    auth/{useSession.ts, RequireAuth.tsx}
    components/ui/*         # button,input,textarea,select,badge,dialog,table,tabs,toast,spinner
    components/{AppLayout.tsx, NavSidebar.tsx, ErrorToast.tsx, DiffView.tsx, ConfirmDialog.tsx}
    features/profiles/{hooks.ts, ProfilesListPage.tsx, NewProfileDialog.tsx}
    features/editor/{hooks.ts, ProfileEditorPage.tsx, PublishDialog.tsx, sections/*Section.tsx}
    features/versions/{hooks.ts, VersionHistoryPage.tsx}
    features/elders/{hooks.ts, EldersPage.tsx}
    features/defaults/DefaultsPage.tsx
    features/audit/{hooks.ts, AuditPage.tsx}
    features/adminUsers/{hooks.ts, AdminUsersPage.tsx}
    routes.tsx
    test/{agentConfigSchema.test.ts, api.test.ts, DiffView.test.tsx, PublishDialog.test.tsx}
  e2e/smoke.spec.ts         # Playwright, authored; run in CI/P5
```

## Task B1: Scaffold + tooling

- [ ] **Step 1:** `package.json` (scripts: dev/build/preview/lint/typecheck/test). deps: @monaco-editor/react, @tanstack/react-query, react, react-dom, react-hook-form, react-router-dom, zod. devDeps: @testing-library/{jest-dom,react,user-event}, @types/react(-dom), @typescript-eslint/*, @vitejs/plugin-react, autoprefixer, eslint, eslint-plugin-react-hooks, jsdom, postcss, tailwindcss, typescript, vite, vitest. Pin to what `npm install` resolves; commit the lockfile. If React 19 peer conflicts surface, fall back to React 18.3 (+ matching `@types/react`).
- [ ] **Step 2:** `vite.config.ts` — `@vitejs/plugin-react`; dev `server.proxy["/v1"] = { target: "http://localhost:8000", changeOrigin: true }`; `build.outDir = "dist"`.
- [ ] **Step 3:** `tsconfig.json` strict (`strict`, `noUncheckedIndexedAccess`, `jsx: react-jsx`); `tailwind.config.js` (content: index.html + src); `postcss.config.js`; `index.html`; `.eslintrc.cjs` (ts + react-hooks, max-warnings 0); `.gitignore` (node_modules, dist); `vitest.config.ts` (jsdom + setup); `vitest.setup.ts` (`@testing-library/jest-dom`).
- [ ] **Step 4:** `npm install`; `npm run build` on a stub `App` → green. Commit configs + lockfile.

## Task B2: API client, types, query client

- [ ] **Step 1:** `src/lib/api.ts`:

```ts
export class ApiError extends Error {
  constructor(public status: number, public detail: string) {
    super(detail);
  }
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    credentials: "include",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    window.location.assign("/v1/auth/login");
    throw new ApiError(401, "redirecting to login");
  }
  if (!res.ok) {
    const detail = await res
      .json()
      .then((b) => b?.detail ?? res.statusText)
      .catch(() => res.statusText);
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(u: string) => request<T>("GET", u),
  post: <T>(u: string, b?: unknown) => request<T>("POST", u, b),
  put: <T>(u: string, b?: unknown) => request<T>("PUT", u, b),
  del: <T>(u: string) => request<T>("DELETE", u),
};
```

- [ ] **Step 2:** `src/types/api.ts` — TS mirrors: `Me {email; role:"admin"|"viewer"}`, `ProfileSummary`, `ProfileDetail`, `VersionSummary`, `VersionDetail`, `AgentConfig` + 8 sub-configs (exact nullability: `tts_model: string|null`, `temperature: number|null`, etc.), `AuditEntry`, `ElderSummary`, `AdminUser`, request bodies (`ProfileCreate`, `DraftUpdate`, `PublishRequest`, `SetDefaultRequest`, `AssignProfileRequest`, `AdminUserCreate`).
- [ ] **Step 3:** `src/lib/queryClient.ts` — `retry:false` (don't mask the 401 redirect), `staleTime:10_000`.
- [ ] **Step 4:** Vitest `src/test/api.test.ts` — mock `fetch`/`window.location`: 401→assign login; 409→`ApiError` with parsed detail; 204→undefined. Commit.

## Task B3: Auth + layout + router

- [ ] **Step 1:** `auth/useSession.ts` (`useQuery(["me"], GET /v1/auth/me)`), `RequireAuth` (splash while loading; the `api` wrapper handles 401), `useIsAdmin()`.
- [ ] **Step 2:** `AppLayout` + `NavSidebar` (Profiles, Elders, Defaults, Audit, Admin Users[admin], operator email + Logout → `POST /v1/auth/logout` then redirect to `/v1/auth/login`).
- [ ] **Step 3:** `routes.tsx` + `main.tsx`: `createBrowserRouter`, `RequireAuth`-wrapped: `/`, `/profiles/:id`, `/profiles/:id/versions`, `/elders`, `/defaults`, `/audit`, `/admin-users`. Wrap in `QueryClientProvider`; mount global `ErrorToast`.
- [ ] **Step 4:** `npm run build` green; commit.

## Task B4: Zod config schema + field metadata

- [ ] **Step 1:** `config/agentConfigSchema.ts` — Zod mirror of `agent_config.py`: prompt length caps + `noBraces` refine on the 7 non-template prompts; template allowed-slot refine (`elder_name`,`last_check_in_line`); voice.speed 0.25–4.0; llm.temperature 0–2; timing 5–180 / 60–7200; tools ⊆ {log_wellness,log_medication,get_today_meds,end_call}; voicemail.window_s 0.5–30; speech-advanced ranges + `min<=max` endpointing refine + `turn_detection` enum. Export `agentConfigSchema`, `type AgentConfigForm`.
- [ ] **Step 2:** `config/fieldMeta.ts` — `{label, help, advanced?}` per field; drives section rendering + reset-to-default for advanced knobs.
- [ ] **Step 3:** Vitest `src/test/agentConfigSchema.test.ts` — valid passes; brace in greeting fails; bad template slot fails; speed=5 fails; min>max endpointing fails; unknown tool fails. Commit.

## Task B5: DiffView + shared UI

- [ ] **Step 1:** `components/ui/*` — minimal Tailwind primitives (no extra deps).
- [ ] **Step 2:** `components/DiffView.tsx` — flatten two `AgentConfig`s to dotted paths; render added/removed/changed (old→new). Pure.
- [ ] **Step 3:** `ConfirmDialog.tsx`, `ErrorToast.tsx` (tiny toast store; mutation `onError` pushes `ApiError.detail`).
- [ ] **Step 4:** Vitest `src/test/DiffView.test.tsx` — editing greeting + max_call_duration_s → exactly 2 rows, correct old/new. Commit.

## Task B6: Profiles list + create/clone

- [ ] **Step 1:** `features/profiles/hooks.ts` — `useProfiles`, `useCreateProfile` (invalidate list), `useArchiveProfile`, `useSetDefault`.
- [ ] **Step 2:** `ProfilesListPage.tsx` — table (name, status, default in/out badges, live version#, unpublished-draft dot, assigned-elder count); row→editor; New/Clone (`NewProfileDialog`, optional clone_from)/Archive (confirm; surface 409); admin-only actions via `useIsAdmin`.
- [ ] **Step 3:** Commit.

## Task B7: Profile editor (core) + publish

- [ ] **Step 1:** `features/editor/hooks.ts` — `useProfile(id)`, `useSaveDraft(id)`, `usePublish(id)`, `useVersion(id,n)`.
- [ ] **Step 2:** `ProfileEditorPage.tsx` — react-hook-form from `draft_config`, `zodResolver(agentConfigSchema)`; left section nav (Prompts·Voice·LLM·STT·Speech·Timing·Tools·Voicemail); sticky header: draft status, Save draft, Publish; map server 422 → `setError`. `sections/*Section.tsx` render each bundle from `fieldMeta`; Prompts uses Monaco (lazy) + textarea fallback, template shows allowed `{slots}`; SpeechAdvanced is a collapsed "Advanced — can degrade call quality" panel with per-field default + reset.
- [ ] **Step 3:** `PublishDialog.tsx` — fetch current live version (if any) → `DiffView(live, draft)` (or "first publish"); optional note; confirm → `usePublish`.
- [ ] **Step 4:** Vitest `src/test/PublishDialog.test.tsx` — renders diff; confirm calls mutation with the note. Commit.

## Task B8: Version history + rollback

- [ ] **Step 1:** `features/versions/hooks.ts` — `useVersions`, `useVersion`, `useRollback` (invalidate profile + versions).
- [ ] **Step 2:** `VersionHistoryPage.tsx` — table (version, who, when, note); select two → `DiffView`; Rollback w/ confirm (surface errors). Commit.

## Task B9: Elders, Defaults, Audit, Admin Users

- [ ] **Step 1: Elders** — `hooks.ts` (`useElders`, `useAssignProfile`); `EldersPage.tsx` (name, masked phone, assigned-profile dropdown from `useProfiles`, "— none —" clears). Admin-only.
- [ ] **Step 2: Defaults** — `DefaultsPage.tsx` (current default in/out from the list; two selects → `useSetDefault`).
- [ ] **Step 3: Audit** — `hooks.ts` (`useAudit(limit)`); `AuditPage.tsx` (when, actor, action, entity, detail; client-side actor/action filter + limit selector).
- [ ] **Step 4: Admin Users** — `hooks.ts` (`useAdminUsers`, `useAddAdminUser`, `useRemoveAdminUser`); `AdminUsersPage.tsx` (list + add[email,role] + remove[confirm, surface 409 last-admin]). Whole screen admin-only.
- [ ] **Step 5:** Commit.

## Task B10: E2E smoke (authored) + final verification

- [ ] **Step 1:** `e2e/smoke.spec.ts` — Playwright login→list→edit→publish→rollback; `test.describe.skip` unless `E2E=1`. Document it runs in CI/P5.
- [ ] **Step 2:** `npm run lint && npm run typecheck && npm run test && npm run build` → all green; `dist/` builds.
- [ ] **Step 3:** Re-run API suite (`uv run pytest -q`) + `ruff`/`mypy` clean.
- [ ] **Step 4:** `.gitignore` excludes `node_modules` + `dist` (don't commit build output). Commit.

---

## Self-Review

**Spec coverage (design §10):** profiles list (B6), editor all bundles + Monaco + advanced panel (B7), publish-diff (B7), version history + diff + rollback (B8), elders/assignment (B9+A2), defaults (B9), audit (B9+A1), admin users (B9), auth/session + 401-redirect (B3); safety affordances: publish diff+confirm, advanced collapsed+reset, destructive confirms surfacing server guard errors (B5/B7/B9).

**Contract accuracy:** TS types (B2) + Zod (B4) mirror the actual `apps/api` schemas read for this plan (`agent_profile.py`, `agent_config.py`, `auth.py`, Part-1). Editor round-trips `ProfileDetail.draft_config` → `PUT /draft` unchanged in shape.

**Placeholder scan:** none — A2 inlines `_mask` rather than guessing a module.

**Out of scope (P5):** static-serving container, Caddyfile site + CIDR, Terraform DNS + vars, CI build job, secret seeding, OAuth redirect-URI registration. Cartesia voices proxy intentionally omitted (free-text voice id, design §12).
