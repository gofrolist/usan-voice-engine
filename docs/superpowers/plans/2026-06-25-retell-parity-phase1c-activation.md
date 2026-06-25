# RetellAI Parity Phase 1c — Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the RetellAI-compat surface safely operable in prod — a super-admin Compat Keys UI, a TOCTOU-free (IP-pinned) webhook delivery path on both senders, and templated `COMPAT_*` settings + an activation runbook.

**Architecture:** Three independent slices on one branch. (A) A new super-admin admin-UI screen built only against the already-shipped `POST/GET/DELETE /v1/admin/compat-keys` (no backend change). (B) A shared `ssrf_guard.pin_to_ip` helper + `resolve_public_or_raise` returning the validated IPs, wired into both `webhook_delivery.py` (native) and `compat/webhook_delivery.py` (compat) so the socket connects to the exact vetted IP while Host + TLS SNI/cert keep the original hostname. (C) The 5 `COMPAT_*` settings templated through compose + the prod `.env` example + docs + runbook.

**Tech Stack:** Python 3.14 / FastAPI / httpx **0.28.1** / pytest (api); React 19.2 / TypeScript 6.0 / react-query 5.101 / vitest 4.1 (admin-ui); Docker Compose (infra).

**Spec:** `docs/superpowers/specs/2026-06-25-retell-parity-phase1c-activation-design.md`

## Global Constraints

- **No new DB migration.** `compat_api_keys` already exists (migration `0036`); nothing in 1c changes schema.
- **No backend change for the keys UI.** The three endpoints exist and are correct: `POST /v1/admin/compat-keys` → `CompatKeyCreatedResponse` (`{id, key_prefix, status, label, created_at, revoked_at, last_used_at, token}`, 201, token shown once), `GET /v1/admin/compat-keys` → `CompatKeyResponse[]`, `DELETE /v1/admin/compat-keys/{key_id}` → 204. All `require_super_admin`, org-scoped to the active org.
- **httpx is 0.28.1.** The IP-pin uses the per-request `sni_hostname` extension + an explicit `Host` header so TLS SNI and certificate verification still use the original hostname while the TCP connect targets the validated IP literal (no second DNS lookup). httpx respects an explicit `Host` header (it only auto-adds one when absent).
- **SSRF fails closed.** Empty/non-global resolution raises `SsrfBlocked`; compat additionally fails closed when the host is absent from `COMPAT_WEBHOOK_ALLOWED_HOSTS` (already implemented in `_guard_host`).
- **No PHI in logs/errors.** Webhook delivery logs bind ids only — never the URL or `str(exc)`; `last_error` stores the exception **type name** only. Do not weaken this.
- **Ship-inert.** All 5 `COMPAT_*` default OFF/empty (`COMPAT_DOCS_ENABLED=false`, `COMPAT_WEBHOOK_ALLOWED_HOSTS=""`, `COMPAT_DEFAULT_TIMEZONE=America/New_York`, `COMPAT_KEY_RATE_LIMIT=600/minute`, `COMPAT_WEBHOOK_DELIVERY_ENABLED=false`). No `docker-compose.prod.yml` override needed — base `environment:` passthrough is inherited by the prod overlay.
- **Test gate:** api — `ruff check . && ruff format --check . && uv run mypy . && uv run pytest` (parallel default; `-n0` for `-s`/pdb). admin-ui — `npm run typecheck` **and** `npm run build` **and** `npx vitest run` (CI runs typecheck + build; local lint/vitest do NOT typecheck). `npm run build` is `tsc --noEmit && vite build`.
- **End state:** squash-merge to `main`. **Do NOT cut a `v*` tag.** The operator deploys later via the runbook.
- **Commit format:** `type(scope): description`, scopes `api`/`admin-ui`/`infra`/`docs`. Attribution is disabled — NO `Co-Authored-By`, NO tool footer.
- **GateGuard hook** intercepts the first Bash + every Write/Edit per subagent: state the requested facts in text, then retry the identical call. Forewarn each implementer/fixer.

---

### Task 1: Compat Keys super-admin screen (admin-ui)

**Files:**
- Create: `apps/admin-ui/src/features/compat-keys/hooks.ts`
- Create: `apps/admin-ui/src/features/compat-keys/CreatedKeyDialog.tsx`
- Create: `apps/admin-ui/src/features/compat-keys/CompatKeysPage.tsx`
- Create: `apps/admin-ui/src/test/CompatKeysPage.test.tsx`
- Modify: `apps/admin-ui/src/types/api.ts` (append the two interfaces)
- Modify: `apps/admin-ui/src/components/nav-icons.tsx` (add `CompatKeysIcon`)
- Modify: `apps/admin-ui/src/components/NavSidebar.tsx` (import + System nav item)
- Modify: `apps/admin-ui/src/routes.tsx` (import + `RequireSuperAdmin` route)

**Interfaces:**
- Consumes: `api.get/post/del` from `lib/api`; `useSession` from `auth/useSession`; `pushToast` from `components/ui/toast`; `Table/Thead/Tbody/Tr/Th/Td`, `Input`, `Button`, `Spinner`, `Dialog`, `ConfirmDialog`; `RequireSuperAdmin` from `auth/RequireTier`.
- Produces: route `/compat-keys`; query key `["compat-keys"]`.

- [ ] **Step 1: Write the failing test** — `apps/admin-ui/src/test/CompatKeysPage.test.tsx`

```tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { CompatKey, Me } from "../types/api";
import { meFixture } from "./meFixture";

const getMock = vi.fn();
const postMock = vi.fn();
const delMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b?: unknown) => postMock(u, b),
    del: (u: string) => delMock(u),
  },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));

const pushToastMock = vi.fn();
vi.mock("../components/ui/toast", () => ({
  pushToast: (message: string, tone?: string) => pushToastMock(message, tone),
}));

import { CompatKeysPage } from "../features/compat-keys/CompatKeysPage";

let me: Me = superAdmin();
let keys: CompatKey[] = [];

// A super-admin (act-as-only, no org membership) — mirrors the OrgConsole fixture.
function superAdmin(): Me {
  return {
    email: "root@example.com",
    is_super_admin: true,
    acting_as: false,
    active_org: null,
    orgs: [],
  };
}

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") return Promise.resolve(me);
  if (url === "/v1/admin/compat-keys") return Promise.resolve(keys);
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function key(over: Partial<CompatKey> = {}): CompatKey {
  return {
    id: "00000000-0000-0000-0000-0000000000c1",
    key_prefix: "key_ab12",
    status: "active",
    label: "Acme CRM",
    created_at: "2026-06-25T12:00:00Z",
    revoked_at: null,
    last_used_at: null,
    ...over,
  };
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <CompatKeysPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CompatKeysPage", () => {
  beforeEach(() => {
    getMock.mockReset();
    postMock.mockReset();
    delMock.mockReset();
    pushToastMock.mockReset();
    getMock.mockImplementation(routeGet);
    me = superAdmin();
    keys = [key()];
  });
  afterEach(() => vi.clearAllMocks());

  it("renders the key list for a super-admin", async () => {
    keys = [key({ key_prefix: "key_zz99", label: "CRM" })];
    renderPage();
    expect(await screen.findByText("key_zz99…")).toBeInTheDocument();
    expect(screen.getByText("CRM")).toBeInTheDocument();
    expect(getMock).toHaveBeenCalledWith("/v1/admin/compat-keys");
  });

  it("shows 'Super-admins only.' for a non-super-admin", async () => {
    me = meFixture("admin");
    renderPage();
    expect(await screen.findByText("Super-admins only.")).toBeInTheDocument();
    expect(getMock).not.toHaveBeenCalledWith("/v1/admin/compat-keys");
  });

  it("creates a key, shows the token once, then closes the dialog", async () => {
    keys = [];
    postMock.mockResolvedValue(key({ token: "key_secret_plaintext_value", label: "Acme CRM" }));
    renderPage();
    await userEvent.type(await screen.findByLabelText("Label (optional)"), "Acme CRM");
    await userEvent.click(screen.getByRole("button", { name: "Create key" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/compat-keys", { label: "Acme CRM" }),
    );
    expect(await screen.findByText("key_secret_plaintext_value")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() =>
      expect(screen.queryByText("key_secret_plaintext_value")).not.toBeInTheDocument(),
    );
  });

  it("omits the label (null) when left blank", async () => {
    keys = [];
    postMock.mockResolvedValue(key({ token: "key_x" }));
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Create key" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/compat-keys", { label: null }),
    );
  });

  it("revokes a key after confirmation", async () => {
    keys = [key({ id: "11111111-1111-1111-1111-111111111111", key_prefix: "key_rv01" })];
    delMock.mockResolvedValue(undefined);
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Revoke key_rv01" }));
    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    await waitFor(() =>
      expect(delMock).toHaveBeenCalledWith(
        "/v1/admin/compat-keys/11111111-1111-1111-1111-111111111111",
      ),
    );
  });

  it("surfaces a create error (no active org) as a toast", async () => {
    const { ApiError } = await import("../lib/api");
    keys = [];
    postMock.mockRejectedValue(new ApiError(409, "no active organization"));
    renderPage();
    await userEvent.type(await screen.findByLabelText("Label (optional)"), "X");
    await userEvent.click(screen.getByRole("button", { name: "Create key" }));
    await waitFor(() =>
      expect(pushToastMock).toHaveBeenCalledWith("no active organization", undefined),
    );
  });
});
```

- [ ] **Step 2: Run the test, watch it fail**

Run: `cd apps/admin-ui && npx vitest run src/test/CompatKeysPage.test.tsx`
Expected: FAIL — `CompatKeysPage` / `CompatKey` not found.

- [ ] **Step 3: Add the types** — append to `apps/admin-ui/src/types/api.ts`

```ts
// RetellAI-compat API keys (super-admin; /v1/admin/compat-keys). Mirrors
// apps/api schemas/compat_api_keys.py — CompatKeyResponse + the create-only token.
export interface CompatKey {
  id: string;
  key_prefix: string;
  status: string;
  label: string | null;
  created_at: string;
  revoked_at: string | null;
  last_used_at: string | null;
}

export interface CompatKeyCreated extends CompatKey {
  token: string;
}
```

- [ ] **Step 4: Add the hooks** — `apps/admin-ui/src/features/compat-keys/hooks.ts`

```ts
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { CompatKey, CompatKeyCreated } from "../../types/api";

// Super-admin only (route + server both gate). The list query stays disabled for
// non-super-admins so we never fire a guaranteed 403.
export function useCompatKeys(enabled: boolean) {
  return useQuery<CompatKey[]>({
    queryKey: ["compat-keys"],
    queryFn: () => api.get<CompatKey[]>("/v1/admin/compat-keys"),
    enabled,
  });
}

// Create returns the plaintext token ONCE (CompatKeyCreated.token). The caller holds
// it in component state to show the once-only dialog, then the list invalidates.
export function useCreateCompatKey() {
  const qc = useQueryClient();
  return useMutation<CompatKeyCreated, ApiError, { label: string | null }>({
    mutationFn: (body) => api.post<CompatKeyCreated>("/v1/admin/compat-keys", body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["compat-keys"] });
    },
    onError: (err) => pushToast(err.detail),
  });
}

// Revoke is a 204 (api.del returns undefined). One-way; the list refetches.
export function useRevokeCompatKey() {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.del<void>(`/v1/admin/compat-keys/${id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["compat-keys"] });
    },
    onError: (err) => pushToast(err.detail),
  });
}
```

- [ ] **Step 5: Add the once-only token dialog** — `apps/admin-ui/src/features/compat-keys/CreatedKeyDialog.tsx`

```tsx
import { Dialog } from "../../components/ui/dialog";
import { Button } from "../../components/ui/button";
import { pushToast } from "../../components/ui/toast";
import type { CompatKeyCreated } from "../../types/api";

// One-time reveal of a freshly created key's plaintext token. The server stores only a
// sha256 hash, so the token is unrecoverable after this closes — the only safe action is
// copy-and-store now; closing requires an explicit "Done", not a stray backdrop dismiss.
async function copyToken(token: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(token);
    pushToast("Token copied to clipboard", "info");
  } catch {
    pushToast("Copy failed — select and copy the token manually");
  }
}

export function CreatedKeyDialog({
  created,
  onDone,
}: {
  created: CompatKeyCreated | null;
  onDone: () => void;
}) {
  return (
    <Dialog open={created !== null} onClose={onDone} title="API key created">
      {created !== null ? (
        <div className="space-y-3">
          <p className="text-sm text-slate-700">
            Copy this token now and store it securely. For your safety it will{" "}
            <strong>never be shown again</strong> — if you lose it, revoke this key and create
            a new one.
          </p>
          <div className="flex items-center gap-2">
            <code className="flex-1 select-all overflow-x-auto rounded-lg border border-line bg-surface-2 px-3 py-2 font-mono text-xs text-ink">
              {created.token}
            </code>
            <Button variant="secondary" onClick={() => void copyToken(created.token)}>
              Copy
            </Button>
          </div>
          <div className="mt-2 flex justify-end">
            <Button onClick={onDone}>Done</Button>
          </div>
        </div>
      ) : null}
    </Dialog>
  );
}
```

- [ ] **Step 6: Add the page** — `apps/admin-ui/src/features/compat-keys/CompatKeysPage.tsx`

```tsx
import { useState, type FormEvent } from "react";
import { Table, Tbody, Td, Th, Thead, Tr } from "../../components/ui/table";
import { Input } from "../../components/ui/input";
import { Button } from "../../components/ui/button";
import { Spinner } from "../../components/ui/spinner";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { useSession } from "../../auth/useSession";
import { useCompatKeys, useCreateCompatKey, useRevokeCompatKey } from "./hooks";
import { CreatedKeyDialog } from "./CreatedKeyDialog";
import type { CompatKey, CompatKeyCreated } from "../../types/api";

// Super-admin screen to mint, list, and revoke RetellAI-compat API keys. A key is how a
// RetellAI client authenticates against our compat surface; it is scoped to the ACTIVE org
// (the super-admin "acts as" the target org first). The server enforces super-admin (403)
// and org scope; the list query stays disabled for non-super-admins.
function formatTs(iso: string | null): string {
  return iso === null ? "—" : new Date(iso).toLocaleString();
}

export function CompatKeysPage() {
  const { data: me } = useSession();
  const isSuperAdmin = !!me?.is_super_admin;
  const keys = useCompatKeys(isSuperAdmin);
  const create = useCreateCompatKey();
  const revoke = useRevokeCompatKey();

  const [label, setLabel] = useState("");
  const [created, setCreated] = useState<CompatKeyCreated | null>(null);
  const [toRevoke, setToRevoke] = useState<CompatKey | null>(null);

  if (!isSuperAdmin) {
    return <p className="text-sm text-slate-600">Super-admins only.</p>;
  }

  function handleCreate(e: FormEvent): void {
    e.preventDefault();
    const trimmed = label.trim();
    create.mutate(
      { label: trimmed.length > 0 ? trimmed : null },
      {
        onSuccess: (createdKey) => {
          setLabel("");
          setCreated(createdKey);
        },
      },
    );
  }

  if (keys.isLoading) {
    return (
      <div className="flex items-center gap-2 text-slate-600">
        <Spinner /> Loading API keys…
      </div>
    );
  }
  if (keys.isError) {
    return (
      <p className="text-sm text-red-700">
        Failed to load API keys: {(keys.error as Error)?.message}
      </p>
    );
  }

  const list = keys.data ?? [];

  return (
    <div className="space-y-4">
      <h1 className="font-display text-2xl text-ink-strong">Compat API Keys</h1>
      <p className="text-sm text-slate-600">
        Keys authenticate RetellAI-compatible clients against{" "}
        <span className="font-medium">{me?.active_org?.name ?? "the active organization"}</span>.
        The token is shown once at creation.
      </p>

      <form
        onSubmit={handleCreate}
        className="flex flex-wrap items-end gap-3 rounded-xl border border-line bg-surface p-4 shadow-card"
      >
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-600" htmlFor="k-label">
            Label (optional)
          </label>
          <Input
            id="k-label"
            className="w-72"
            placeholder="e.g. Acme CRM production"
            maxLength={200}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
        </div>
        <Button type="submit" disabled={create.isPending}>
          {create.isPending ? "Creating…" : "Create key"}
        </Button>
      </form>

      <Table>
        <Thead>
          <Tr>
            <Th>Key</Th>
            <Th>Label</Th>
            <Th>Status</Th>
            <Th>Created</Th>
            <Th>Last used</Th>
            <Th className="text-right">Actions</Th>
          </Tr>
        </Thead>
        <Tbody>
          {list.length === 0 ? (
            <Tr>
              <Td className="text-slate-500" colSpan={6}>
                No API keys.
              </Td>
            </Tr>
          ) : null}
          {list.map((k) => (
            <Tr key={k.id}>
              <Td className="font-mono text-xs">{k.key_prefix}…</Td>
              <Td className="text-slate-600">{k.label ?? "—"}</Td>
              <Td className="text-xs uppercase tracking-wide text-slate-500">{k.status}</Td>
              <Td className="text-slate-500">{formatTs(k.created_at)}</Td>
              <Td className="text-slate-500">{formatTs(k.last_used_at)}</Td>
              <Td className="text-right">
                {k.status === "active" ? (
                  <Button
                    variant="danger"
                    aria-label={`Revoke ${k.key_prefix}`}
                    disabled={revoke.isPending}
                    onClick={() => setToRevoke(k)}
                  >
                    Revoke
                  </Button>
                ) : null}
              </Td>
            </Tr>
          ))}
        </Tbody>
      </Table>

      <CreatedKeyDialog created={created} onDone={() => setCreated(null)} />

      <ConfirmDialog
        open={toRevoke !== null}
        title="Revoke API key?"
        body={
          <>
            Any client using <code className="font-mono">{toRevoke?.key_prefix}…</code> will
            immediately lose access. This cannot be undone.
          </>
        }
        confirmLabel="Revoke"
        busy={revoke.isPending}
        onConfirm={() => {
          if (toRevoke !== null) {
            revoke.mutate(toRevoke.id, { onSuccess: () => setToRevoke(null) });
          }
        }}
        onCancel={() => setToRevoke(null)}
      />
    </div>
  );
}
```

- [ ] **Step 7: Run the test, watch it pass**

Run: `cd apps/admin-ui && npx vitest run src/test/CompatKeysPage.test.tsx`
Expected: PASS (6 tests).

- [ ] **Step 8: Wire the nav icon** — `apps/admin-ui/src/components/nav-icons.tsx`

Open the file. Copy the existing `OrganizationsIcon` component **verbatim**, rename the copy to `CompatKeysIcon`, and replace ONLY its inner SVG path element(s) with the key path below — keep the outer `<svg>` wrapper (viewBox, width/height, stroke, fill, props) byte-identical to its siblings so size/stroke match:

```tsx
<path d="m21 2-2 2m-3.5 3.5L19 4m-3.5 3.5a5.5 5.5 0 1 1-7.778 7.778A5.5 5.5 0 0 1 15.5 7.5m0 0 3 3" />
```

- [ ] **Step 9: Wire the nav item** — `apps/admin-ui/src/components/NavSidebar.tsx`

Add `CompatKeysIcon` to the icon import from `"./nav-icons"`, then add this item to the **System** group's `items` array (after the `organizations` entry):

```ts
      {
        to: "/compat-keys",
        label: "Compat API Keys",
        icon: CompatKeysIcon,
        superAdminOnly: true,
      },
```

- [ ] **Step 10: Wire the route** — `apps/admin-ui/src/routes.tsx`

Add the import next to the other feature pages:

```ts
import { CompatKeysPage } from "./features/compat-keys/CompatKeysPage";
```

Add this route object immediately after the `organizations` route object (inside the `PageLayout` children array):

```tsx
          {
            path: "compat-keys",
            element: (
              <RequireSuperAdmin>
                <CompatKeysPage />
              </RequireSuperAdmin>
            ),
          },
```

- [ ] **Step 11: Run the full admin-ui gate**

Run: `cd apps/admin-ui && npm run typecheck && npm run build && npx vitest run`
Expected: typecheck clean, build succeeds, all tests pass. (If `vitest` flakes on a 5000ms timeout under load, re-run / isolate — pre-existing flakiness, not a regression.)

- [ ] **Step 12: Commit**

```bash
git add apps/admin-ui/src/features/compat-keys apps/admin-ui/src/test/CompatKeysPage.test.tsx \
  apps/admin-ui/src/types/api.ts apps/admin-ui/src/components/nav-icons.tsx \
  apps/admin-ui/src/components/NavSidebar.tsx apps/admin-ui/src/routes.tsx
git commit -m "feat(admin-ui): super-admin Compat API Keys management screen"
```

---

### Task 2: SSRF core — `pin_to_ip` + `resolve_public_or_raise` returns validated IPs (api)

**Files:**
- Modify: `apps/api/src/usan_api/ssrf_guard.py`
- Test: `apps/api/tests/test_ssrf_guard.py`

**Interfaces:**
- Produces: `async def resolve_public_or_raise(host: str) -> list[str]` (was `-> None`; returns the validated addresses) and `def pin_to_ip(url: str, ip: str) -> tuple[str, str, str]` → `(connect_url, host_header, sni_hostname)`. Consumed by Tasks 3 and 4.

- [ ] **Step 1: Write the failing tests** — append to `apps/api/tests/test_ssrf_guard.py`

```python
async def test_resolve_public_or_raise_returns_validated_addrs(monkeypatch: pytest.MonkeyPatch):
    # The pin path needs the validated IPs back so the caller connects to a vetted
    # address rather than letting httpx re-resolve (TOCTOU close, §8.2).
    async def _fake_resolve(host: str) -> list[str]:
        return ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]

    monkeypatch.setattr(ssrf_guard, "_resolve", _fake_resolve)
    addrs = await resolve_public_or_raise("hooks.example.com")
    assert addrs == ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]


@pytest.mark.parametrize(
    ("url", "ip", "expected_url", "expected_host", "expected_sni"),
    [
        # IPv4, path + query preserved, default port
        (
            "https://hooks.example.com/sink?token=abc",
            "93.184.216.34",
            "https://93.184.216.34/sink?token=abc",
            "hooks.example.com",
            "hooks.example.com",
        ),
        # explicit non-default port preserved on both connect URL and Host header
        (
            "https://hooks.example.com:8443/retell",
            "93.184.216.34",
            "https://93.184.216.34:8443/retell",
            "hooks.example.com:8443",
            "hooks.example.com",
        ),
        # IPv6 is bracketed in the connect URL; Host/SNI stay the hostname
        (
            "https://hooks.example.com/",
            "2606:2800:220:1:248:1893:25c8:1946",
            "https://[2606:2800:220:1:248:1893:25c8:1946]/",
            "hooks.example.com",
            "hooks.example.com",
        ),
    ],
)
def test_pin_to_ip(url: str, ip: str, expected_url: str, expected_host: str, expected_sni: str):
    connect_url, host_header, sni = ssrf_guard.pin_to_ip(url, ip)
    assert connect_url == expected_url
    assert host_header == expected_host
    assert sni == expected_sni
```

- [ ] **Step 2: Run the tests, watch them fail**

Run: `cd apps/api && uv run pytest -n0 tests/test_ssrf_guard.py -k "returns_validated_addrs or pin_to_ip" -q`
Expected: FAIL — `pin_to_ip` not defined; `resolve_public_or_raise` returns `None`.

- [ ] **Step 3: Implement** — edit `apps/api/src/usan_api/ssrf_guard.py`

Change the import line:

```python
from urllib.parse import urlsplit, urlunsplit
```

Change `resolve_public_or_raise` to return the validated addresses (keep the fail-closed body byte-for-byte; only the signature, docstring, and the final `return` change):

```python
async def resolve_public_or_raise(host: str) -> list[str]:
    """Delivery-time SSRF gate (layer 2, spec §8.2), run before EVERY POST.

    Fails closed: at least one address must resolve AND every resolved address
    must be globally routable. Returns the validated addresses so the caller can
    PIN the connection to a vetted IP (see ``pin_to_ip``), closing the
    resolve-then-connect TOCTOU instead of letting httpx re-resolve at connect.
    """
    addrs = await _resolve(host)
    if not addrs:
        raise SsrfBlocked("DNS resolution returned no addresses")
    for addr in addrs:
        ip: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(addr)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if not ip.is_global:
            raise SsrfBlocked("resolved address is not globally routable")
    return addrs
```

Add `pin_to_ip` at the end of the module:

```python
def pin_to_ip(url: str, ip: str) -> tuple[str, str, str]:
    """Pin a validated webhook URL to a vetted IP, closing the SSRF TOCTOU.

    Returns ``(connect_url, host_header, sni_hostname)``:
    - ``connect_url`` is ``url`` with its host replaced by ``ip`` (an IP literal,
      so httpx connects WITHOUT a second DNS lookup); scheme, port, path, and
      query are preserved, IPv6 is bracketed.
    - ``host_header`` is the original host (with port if explicit) for the HTTP
      ``Host`` header, so request routing is unchanged.
    - ``sni_hostname`` is the original hostname for the ``sni_hostname`` request
      extension, so TLS SNI and certificate verification still use the real name.

    The IP the guard validated is therefore the exact IP the socket connects to.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    literal = f"[{ip}]" if ":" in ip else ip
    netloc = literal if parts.port is None else f"{literal}:{parts.port}"
    connect_url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))
    host_header = host if parts.port is None else f"{host}:{parts.port}"
    return connect_url, host_header, host
```

- [ ] **Step 4: Run the tests, watch them pass**

Run: `cd apps/api && uv run pytest -n0 tests/test_ssrf_guard.py -q`
Expected: PASS (existing matrices + the 2 new tests). The existing `test_resolve_public_or_raise_accepts` still passes (it ignores the return value).

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/ssrf_guard.py apps/api/tests/test_ssrf_guard.py
git commit -m "feat(api): ssrf_guard.pin_to_ip + resolve returns validated IPs (TOCTOU close)"
```

---

### Task 3: Pin native webhook delivery to the validated IP (api)

**Files:**
- Modify: `apps/api/src/usan_api/webhook_delivery.py` (`deliver_one`)
- Test: `apps/api/tests/test_webhook_delivery.py` (new pin test + fix 2 existing host-routing handlers)

**Interfaces:**
- Consumes: `ssrf_guard.resolve_public_or_raise` (now `-> list[str]`) and `ssrf_guard.pin_to_ip` from Task 2.

- [ ] **Step 1: Write the failing test** — append to `apps/api/tests/test_webhook_delivery.py`

```python
async def test_delivery_pins_connection_to_validated_ip(session_factory, monkeypatch):
    # The validated IP — not a hostname httpx re-resolves — is the socket target,
    # closing the resolve-then-connect TOCTOU. _public_resolve yields 93.184.216.34.
    endpoint_id = await _seed_endpoint(session_factory)
    await _seed_delivery(session_factory, endpoint_id)

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    _install_client(monkeypatch, handler)
    stats = await webhook_delivery.poll_once(session_factory, _settings())
    assert stats["delivered"] == 1

    (request,) = seen
    # Connect target is the vetted IP literal (no second DNS lookup); Host header +
    # SNI keep the original hostname for routing and certificate verification.
    assert request.url.host == "93.184.216.34"
    assert request.headers["Host"] == "hooks.example.com"
    assert request.extensions["sni_hostname"] == "hooks.example.com"
    # Pinning leaves the signed body + headers untouched.
    assert request.headers["X-Usan-Event"] == "call.completed"
```

- [ ] **Step 2: Run it, watch it fail**

Run: `cd apps/api && uv run pytest -n0 tests/test_webhook_delivery.py::test_delivery_pins_connection_to_validated_ip -q`
Expected: FAIL — `request.url.host` is still `hooks.example.com`; no `sni_hostname` extension.

- [ ] **Step 3: Implement the pin** — in `apps/api/src/usan_api/webhook_delivery.py::deliver_one`

Inside the `try:` block, capture the validated IPs from `resolve_public_or_raise`:

```python
            # Layer-2 SSRF gate before EVERY POST (spec §8.2): validate AND pin.
            addrs = await ssrf_guard.resolve_public_or_raise(urlsplit(url).hostname or "")
```

…and change the streaming call from:

```python
            async with client.stream("POST", url, content=raw, headers=headers) as response:
```

to:

```python
            connect_url, host_header, sni = ssrf_guard.pin_to_ip(url, addrs[0])
            async with client.stream(
                "POST",
                connect_url,
                content=raw,
                headers={**headers, "Host": host_header},
                extensions={"sni_hostname": sni},
            ) as response:
```

- [ ] **Step 4: Fix the two existing host-routing handlers**

Pinning rewrites `request.url.host` to the IP, so the multi-endpoint tests that branch on the URL host must branch on the **Host header** instead. In `apps/api/tests/test_webhook_delivery.py`, change both occurrences:

- in `test_groups_deliver_concurrently_ordered_within` (~line 459): `if request.url.host == "b.example.com":` → `if request.headers["Host"] == "b.example.com":`
- in `test_one_bad_group_does_not_abort_other_groups` (~line 489): `if request.url.host == "bad.example.com":` → `if request.headers["Host"] == "bad.example.com":`

- [ ] **Step 5: Run the full native delivery suite, watch it pass**

Run: `cd apps/api && uv run pytest -n0 tests/test_webhook_delivery.py -q`
Expected: PASS — the new pin test plus all existing tests (signed-POST, retry, breaker, SSRF-block, grouping). The SSRF-block test still passes because `resolve_public_or_raise` raises before `pin_to_ip` is reached.

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/webhook_delivery.py apps/api/tests/test_webhook_delivery.py
git commit -m "fix(api): pin native webhook delivery to the validated IP (close SSRF TOCTOU)"
```

---

### Task 4: Pin compat webhook delivery + lock the delivery-time allow-list (api)

**Files:**
- Modify: `apps/api/src/usan_api/compat/webhook_delivery.py` (`_guard_host` returns addrs; `deliver_one` pins)
- Test: `apps/api/tests/test_compat_webhooks.py` (update fake client + assertions; add `_guard_host` lock tests)

**Interfaces:**
- Consumes: `ssrf_guard.pin_to_ip` from Task 2.
- Note: `_guard_host` **already** enforces `host not in COMPAT_WEBHOOK_ALLOWED_HOSTS ⇒ SsrfBlocked` before resolving (verified in source). This task changes its return type to `list[str]` and adds tests that LOCK the allow-list behavior — it does not add the allow-list check (it exists).

- [ ] **Step 1: Write the failing tests** — append to `apps/api/tests/test_compat_webhooks.py`

```python
async def test_guard_host_blocks_host_not_in_allowlist(monkeypatch):
    # Delivery-time PHI gate: a registered host that has since left the allow-list is
    # blocked at SEND time (fail-closed), not just at registration.
    async def _fake(_host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_guard, "_resolve", _fake)
    with pytest.raises(ssrf_guard.SsrfBlocked):
        await cwd._guard_host("evil.example.com", frozenset({"hooks.example.com"}))


async def test_guard_host_blocks_when_allowlist_empty():
    # Empty allow-list ⇒ NOTHING leaves (the ship-inert PHI default).
    with pytest.raises(ssrf_guard.SsrfBlocked):
        await cwd._guard_host("hooks.example.com", frozenset())


async def test_guard_host_allows_listed_host_and_returns_addrs(monkeypatch):
    async def _fake(_host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(ssrf_guard, "_resolve", _fake)
    addrs = await cwd._guard_host("hooks.example.com", frozenset({"hooks.example.com"}))
    assert addrs == ["93.184.216.34"]
```

- [ ] **Step 2: Run them, watch them fail**

Run: `cd apps/api && uv run pytest -n0 tests/test_compat_webhooks.py -k _guard_host -q`
Expected: FAIL — `_guard_host` currently returns `None`, so the addrs assertion fails (the two raise-tests may already pass; the returns-addrs test is the failing driver).

- [ ] **Step 3: Make `_guard_host` return the validated addrs** — in `apps/api/src/usan_api/compat/webhook_delivery.py`

```python
async def _guard_host(host: str, allowed: frozenset[str]) -> list[str]:
    """Delivery-time PHI gate: the host MUST be in the allow-list (empty list => nothing
    leaves) AND globally routable. Returns the validated addresses so the caller can pin
    the connection to a vetted IP. Defense-in-depth — registration already gates the host,
    but the allow-list may have shrunk since."""
    if not allowed or host.lower() not in allowed:
        raise SsrfBlocked("compat webhook host not in COMPAT_WEBHOOK_ALLOWED_HOSTS")
    return await ssrf_guard.resolve_public_or_raise(host)
```

- [ ] **Step 4: Pin the compat POST** — in the same file's `deliver_one`

Capture the addrs from `_guard_host` and pin the stream (the rest of the `try` body is unchanged):

```python
            addrs = await _guard_host(host, settings.compat_webhook_allowed_hosts_set)
            body = await _build_body(db, settings, call, claimed.event, client_host=host)
            raw = json.dumps(body, separators=(",", ":")).encode()
            ts_ms = int(time.time() * 1000)
            headers = {
                "Content-Type": "application/json",
                "User-Agent": _USER_AGENT,
                "x-retell-delivery-id": str(claimed.id),
                "x-retell-signature": webhook_signature.signature_header(
                    ts_ms, webhook_signature.sign(secret, raw, ts_ms)
                ),
            }
            connect_url, host_header, sni = ssrf_guard.pin_to_ip(url, addrs[0])
            async with client.stream(
                "POST",
                connect_url,
                content=raw,
                headers={**headers, "Host": host_header},
                extensions={"sni_hostname": sni},
            ) as response:
```

- [ ] **Step 5: Update the fake test client + the pinned-URL assertion**

The test's `_CaptureClient.stream` signature has no `extensions` and the lifecycle test asserts the original URL. Update both in `apps/api/tests/test_compat_webhooks.py`:

Change the fake client's `stream` (~line 85) to accept and record `extensions`:

```python
    def stream(
        self,
        method: str,
        url: str,
        *,
        content: bytes,
        headers: dict[str, str],
        extensions: dict[str, object] | None = None,
    ) -> _Resp:
        self.sink.append(
            {
                "method": method,
                "url": url,
                "content": content,
                "headers": dict(headers),
                "extensions": dict(extensions or {}),
            }
        )
        return _Resp(self.status)
```

In `test_lifecycle_delivers_three_signed_events`, replace the per-request URL assertion (~line 208, `assert req["url"] == _WEBHOOK_URL`) with the pinned-connection assertions:

```python
        # Pinned to the validated IP (_fake_resolve → 93.184.216.34); Host + SNI keep
        # the original allow-listed hostname for routing and certificate verification.
        assert req["url"] == "https://93.184.216.34/retell"
        assert req["headers"]["Host"] == _ALLOWED_HOST
        assert req["extensions"]["sni_hostname"] == _ALLOWED_HOST
```

- [ ] **Step 6: Run the full compat webhook suite, watch it pass**

Run: `cd apps/api && uv run pytest -n0 tests/test_compat_webhooks.py -q`
Expected: PASS — the 3 `_guard_host` lock tests, the updated lifecycle test (now asserting the pin), `test_delivery_id_stable_across_retry`, and the registration-rejection tests.

- [ ] **Step 7: Commit**

```bash
git add apps/api/src/usan_api/compat/webhook_delivery.py apps/api/tests/test_compat_webhooks.py
git commit -m "fix(api): pin compat webhook delivery to validated IP; lock delivery-time allow-list"
```

---

### Task 5: Template the 5 `COMPAT_*` settings + activation runbook (infra/docs)

**Files:**
- Modify: `infra/docker-compose.yml` (api service `environment:`)
- Modify: `infra/.env.prod.example` (new COMPAT section)
- Modify: `infra/README.md` (new subsection)
- Create: `docs/deployment/compat-settings-wiring.md`

**Interfaces:** none (config + docs). `infra/.env` is **gitignored** and is NOT edited — dev relies on the compose `${VAR:-default}` defaults; prod is operator-filled in Secret Manager from `.env.prod.example`.

- [ ] **Step 1: Add the passthrough to compose** — in `infra/docker-compose.yml`, in the `api:` service `environment:` map, after the `INVITE_EMAIL_*` block (and before the `cap_drop:` comment), add:

```yaml
      # RetellAI-compatible public API (feature 003). SHIP-INERT: the compat surface
      # is always mounted but returns 401 until a super-admin mints a compat key, and
      # webhook delivery + docs default OFF. To go live see docs/deployment/compat-settings-wiring.md.
      COMPAT_DOCS_ENABLED: ${COMPAT_DOCS_ENABLED:-false}
      COMPAT_WEBHOOK_ALLOWED_HOSTS: ${COMPAT_WEBHOOK_ALLOWED_HOSTS:-}
      COMPAT_DEFAULT_TIMEZONE: ${COMPAT_DEFAULT_TIMEZONE:-America/New_York}
      COMPAT_KEY_RATE_LIMIT: ${COMPAT_KEY_RATE_LIMIT:-600/minute}
      COMPAT_WEBHOOK_DELIVERY_ENABLED: ${COMPAT_WEBHOOK_DELIVERY_ENABLED:-false}
```

- [ ] **Step 2: Verify compose interpolation**

Run: `cd infra && docker compose -f docker-compose.yml config 2>/dev/null | grep -i COMPAT_`
Expected: all 5 keys appear with resolved values (`false`, empty, `America/New_York`, `600/minute`, `false`). (If Docker is unavailable, instead `grep -c 'COMPAT_' docker-compose.yml` and confirm ≥ 5.)

- [ ] **Step 3: Add the COMPAT section to the prod env example** — in `infra/.env.prod.example`, after the Grafana observability section and before the `# === Admin UI — Google SSO console ===` header, add:

```bash
# === RetellAI-compatible API (feature 003; ships inert) ===
# The compat surface is always mounted but returns 401 until a super-admin mints a
# compat key in the admin UI (System -> Compat API Keys). To enable PHI-bearing webhook
# delivery: set the allow-list to your attested CRM webhook FQDN AND flip the delivery
# flag, then seed BOTH here and the VM .env BEFORE the tag deploy (the deploy never
# re-fetches the secret). See docs/deployment/compat-settings-wiring.md.
COMPAT_DOCS_ENABLED=false
COMPAT_WEBHOOK_ALLOWED_HOSTS=
COMPAT_DEFAULT_TIMEZONE=America/New_York
COMPAT_KEY_RATE_LIMIT=600/minute
COMPAT_WEBHOOK_DELIVERY_ENABLED=false
```

- [ ] **Step 4: Add the wiring + activation doc** — create `docs/deployment/compat-settings-wiring.md`

```markdown
# RetellAI-compat settings wiring & activation

The RetellAI-compatible API (`/compat/*`) ships with the code but is **inert**: it is
always mounted yet returns **401** until a super-admin mints a compat API key. There is
**no master enable flag** — deploying the code activates nothing on its own.

## The 5 settings

| Key | Default | Purpose |
|-----|---------|---------|
| `COMPAT_DOCS_ENABLED` | `false` | Mounts the compat OpenAPI/docs at `/compat/docs` (separate from native `DOCS_ENABLED`). |
| `COMPAT_WEBHOOK_ALLOWED_HOSTS` | `""` | Comma-separated attested FQDNs allowed to receive PHI-bearing compat webhooks. Empty ⇒ nothing leaves (fail-closed), enforced at registration **and** delivery. |
| `COMPAT_DEFAULT_TIMEZONE` | `America/New_York` | Timezone for Contacts the compat layer lazily upserts (RetellAI has no Contact concept). |
| `COMPAT_KEY_RATE_LIMIT` | `600/minute` | Dedicated elevated rate-limit bucket for the compat key. |
| `COMPAT_WEBHOOK_DELIVERY_ENABLED` | `false` | Gates the claim+POST half of the compat webhook poller. Housekeeping runs regardless. |

## The 3-layer plumbing (and the BOTH-places gotcha)

`usan-prod-env` (Secret Manager) → `startup.sh` writes `/opt/usan/infra/.env` on boot →
`docker compose` interpolates → the api container receives the value via `environment:`.

A new key **no-ops unless it is in BOTH** the compose `environment:` map (`infra/docker-compose.yml`)
**and** the VM `.env`. The `v*` tag deploy runs `compose up --env-file infra/.env` and **never
re-fetches the secret**, so any value change must reach the VM `.env` **before** the tag is cut
(reboot to re-fetch via `startup.sh`, or IAP-SSH and edit `/opt/usan/infra/.env` by hand).

## Activation runbook (going live with PHI-bearing webhooks)

1. Deploy the merged code (`git tag vX.Y.Z && git push origin vX.Y.Z`). The surface is still
   key-inert.
2. Admin UI → **System → Compat API Keys** → **Create key** for the client (act-as the target
   org first). Hand off the one-time token securely; it is never shown again.
3. Set `COMPAT_WEBHOOK_ALLOWED_HOSTS=<attested CRM webhook FQDN>` and
   `COMPAT_WEBHOOK_DELIVERY_ENABLED=true` in the filled prod `.env`.
4. Push the filled `.env` to Secret Manager (`gcloud secrets versions add usan-prod-env
   --data-file=…`) **and** refresh the VM `/opt/usan/infra/.env` BEFORE cutting the tag.
5. Verify the api runs as the non-superuser `usan_app` role (RLS enforcing) — the tenant
   isolation guarantee for compat traffic.
6. When shrinking the allow-list later, audit `compat_webhook_endpoints.webhook_url` against the
   new list. Delivery-time re-validation (`_guard_host`) already blocks sends to removed hosts,
   but stale registrations should still be reviewed.

## Why deploying is safe

No key exists by default, so every compat endpoint 401s. Webhook delivery and docs default OFF.
Merging and even deploying the code changes no reachable behavior until step 2 is taken
deliberately by an operator.
```

- [ ] **Step 5: Add the README pointer** — in `infra/README.md`, in the "Production deploy" area (near the Admin UI subsection), add a short subsection:

```markdown
## RetellAI-compat API (feature 003)

The `/compat/*` surface ships inert — always mounted, but 401 until a super-admin mints a
compat key in the admin UI (**System → Compat API Keys**). The 5 `COMPAT_*` settings and the
full activation runbook (issue key → set the attested allow-list host → enable webhook delivery
→ seed Secret Manager + VM `.env` before the tag) live in
[`docs/deployment/compat-settings-wiring.md`](../docs/deployment/compat-settings-wiring.md).
Like every key, a `COMPAT_*` change must reach the VM `.env` **before** the tag deploy — the
deploy does not re-fetch the secret.
```

- [ ] **Step 6: Commit (pre-commit hooks must pass: yaml + whitespace)**

```bash
git add infra/docker-compose.yml infra/.env.prod.example infra/README.md docs/deployment/compat-settings-wiring.md
git commit -m "infra: template COMPAT_* settings + compat activation runbook"
```
Expected: the `check yaml`, trailing-whitespace, and end-of-file pre-commit hooks pass; commit succeeds.

---

## Self-Review

**Spec coverage:** Work item A → Task 1. Work item B (IP-pin both surfaces) → Tasks 2+3+4; B's delivery-time allow-list → Task 4 `_guard_host` lock tests (already-present behavior). Work item C (5 settings + runbook) → Task 5. End state (merge, no tag) → handled by finishing-a-development-branch, not a task.

**Placeholder scan:** the only `<…>` token is `<attested CRM webhook FQDN>` in the runbook — a legitimate operator-supplied prod value, documented as such, not a code placeholder. No TBD/TODO.

**Type/name consistency:** `pin_to_ip(url, ip) -> (connect_url, host_header, sni_hostname)` is defined in Task 2 and consumed identically in Tasks 3 and 4. `resolve_public_or_raise -> list[str]` defined in Task 2, used in 3 (`addrs`) and via `_guard_host` in 4. `CompatKey`/`CompatKeyCreated` defined in Task 1 Step 3 and used by the hooks/page/test. Query key `["compat-keys"]` consistent across hooks. Endpoint paths match the verified backend (`/v1/admin/compat-keys`, 204 on delete).

**Known existing-test breakage handled:** native `request.url.host` handlers (~lines 459/489) → Host header (Task 3 Step 4); compat `_CaptureClient.stream` signature + `req["url"]` assertion (Task 4 Step 5).
