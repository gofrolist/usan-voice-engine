import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { Me, Organization } from "../types/api";
import { meFixture } from "./meFixture";

const getMock = vi.fn();
const postMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b?: unknown) => postMock(u, b),
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

import { OrgConsolePage } from "../features/orgs/OrgConsolePage";

// Whether the current session is a super-admin gates the whole page.
let me: Me = superAdmin();
let orgs: Organization[] = [];

// A super-admin with no org membership (act-as-only) — the org console is theirs.
function superAdmin(): Me {
  return {
    email: "root@example.com",
    is_super_admin: true,
    acting_as: false,
    active_org: null,
    orgs: [],
    version: "dev",
  };
}

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") return Promise.resolve(me);
  if (url === "/v1/admin/organizations") return Promise.resolve(orgs);
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <OrgConsolePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function org(over: Partial<Organization> = {}): Organization {
  return {
    id: "00000000-0000-0000-0000-0000000000b1",
    name: "Beacon Care",
    slug: "beacon-care",
    status: "active",
    ...over,
  };
}

describe("OrgConsolePage", () => {
  beforeEach(() => {
    getMock.mockReset();
    postMock.mockReset();
    pushToastMock.mockReset();
    getMock.mockImplementation(routeGet);
    me = superAdmin();
    orgs = [org()];
  });
  afterEach(() => vi.clearAllMocks());

  it("renders the org list for a super-admin", async () => {
    orgs = [org({ name: "Beacon Care", slug: "beacon-care" })];
    renderPage();
    expect(await screen.findByText("Beacon Care")).toBeInTheDocument();
    expect(screen.getByText("beacon-care")).toBeInTheDocument();
    expect(getMock).toHaveBeenCalledWith("/v1/admin/organizations");
  });

  it("shows 'Super-admins only.' for a non-super-admin", async () => {
    me = meFixture("admin");
    renderPage();
    expect(await screen.findByText("Super-admins only.")).toBeInTheDocument();
    expect(getMock).not.toHaveBeenCalledWith("/v1/admin/organizations");
  });

  it("creates an org via POST /v1/admin/organizations (name + slug + first admin)", async () => {
    orgs = [];
    postMock.mockResolvedValue(org({ name: "New Org", slug: "new-org" }));
    renderPage();
    await userEvent.type(await screen.findByLabelText("Name"), "New Org");
    await userEvent.type(screen.getByLabelText("Slug"), "new-org");
    await userEvent.type(screen.getByLabelText("First admin email"), "admin@new.org");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/organizations", {
        name: "New Org",
        slug: "new-org",
        first_admin_email: "admin@new.org",
      }),
    );
  });

  it("omits first_admin_email when left blank", async () => {
    orgs = [];
    postMock.mockResolvedValue(org({ name: "New Org", slug: "new-org" }));
    renderPage();
    await userEvent.type(await screen.findByLabelText("Name"), "New Org");
    await userEvent.type(screen.getByLabelText("Slug"), "new-org");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/organizations", {
        name: "New Org",
        slug: "new-org",
        first_admin_email: null,
      }),
    );
  });

  it("surfaces a duplicate-slug 409 as a toast", async () => {
    const { ApiError } = await import("../lib/api");
    orgs = [];
    postMock.mockRejectedValue(new ApiError(409, "slug already in use"));
    renderPage();
    await userEvent.type(await screen.findByLabelText("Name"), "Dup Org");
    await userEvent.type(screen.getByLabelText("Slug"), "beacon-care");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(pushToastMock).toHaveBeenCalledWith("slug already in use", undefined),
    );
  });

  it("acts as an org via POST /v1/auth/switch-org from the row button", async () => {
    orgs = [org({ id: "00000000-0000-0000-0000-0000000000b1", name: "Beacon Care" })];
    postMock.mockResolvedValue(superAdmin());
    renderPage();
    await screen.findByText("Beacon Care");
    await userEvent.click(screen.getByRole("button", { name: "Act as Beacon Care" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/auth/switch-org", {
        organization_id: "00000000-0000-0000-0000-0000000000b1",
      }),
    );
  });
});
