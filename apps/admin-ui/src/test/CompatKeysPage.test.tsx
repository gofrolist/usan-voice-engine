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
    postMock.mockResolvedValue({ ...key({ label: "Acme CRM" }), token: "key_secret_plaintext_value" });
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

  it("token dialog stays open on Escape and closes only on explicit Done", async () => {
    keys = [];
    postMock.mockResolvedValue({ ...key({ label: "Escape test" }), token: "key_escape_proof" });
    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: "Create key" }));
    expect(await screen.findByText("key_escape_proof")).toBeInTheDocument();
    await userEvent.keyboard("{Escape}");
    expect(screen.getByText("key_escape_proof")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Done" }));
    await waitFor(() =>
      expect(screen.queryByText("key_escape_proof")).not.toBeInTheDocument(),
    );
  });

  it("omits the label (null) when left blank", async () => {
    keys = [];
    postMock.mockResolvedValue({ ...key(), token: "key_x" });
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
