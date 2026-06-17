import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { Me, Organization } from "../types/api";
import { meFixture } from "./meFixture";

// Route-by-URL api mock. The switcher derives from the real /v1/auth/me query and,
// for a super-admin, the /v1/admin/organizations query (act-as targets).
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

import { OrgSwitcher } from "../components/OrgSwitcher";

let me: Me = meFixture("admin");
let organizations: Organization[] = [];

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") return Promise.resolve(me);
  if (url === "/v1/admin/organizations") return Promise.resolve(organizations);
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function renderSwitcher() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <OrgSwitcher />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  postMock.mockReset();
  pushToastMock.mockReset();
  getMock.mockImplementation(routeGet);
  me = meFixture("admin");
  organizations = [];
});
afterEach(() => vi.clearAllMocks());

describe("OrgSwitcher", () => {
  it("renders the active org name as the selected value", async () => {
    const select = renderSwitcher();
    void select;
    const el = (await screen.findByLabelText("Switch organization")) as HTMLSelectElement;
    expect(el.value).toBe(me.active_org!.id);
    expect(screen.getByRole("option", { name: "Acme Care" })).toBeInTheDocument();
  });

  it("lists every org the caller belongs to", async () => {
    me = meFixture("admin", {
      orgs: [
        { id: "o1", name: "Acme Care", slug: "acme-care", role: "admin" },
        { id: "o2", name: "Beta Health", slug: "beta-health", role: "viewer" },
      ],
      active_org: { id: "o1", name: "Acme Care", slug: "acme-care", role: "admin" },
    });
    renderSwitcher();
    await screen.findByLabelText("Switch organization");
    expect(screen.getByRole("option", { name: "Acme Care" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Beta Health" })).toBeInTheDocument();
  });

  it("posts switch-org with the chosen organization_id", async () => {
    me = meFixture("admin", {
      orgs: [
        { id: "o1", name: "Acme Care", slug: "acme-care", role: "admin" },
        { id: "o2", name: "Beta Health", slug: "beta-health", role: "viewer" },
      ],
      active_org: { id: "o1", name: "Acme Care", slug: "acme-care", role: "admin" },
    });
    postMock.mockResolvedValue({ ...me, orgs: [] });
    renderSwitcher();
    const select = (await screen.findByLabelText("Switch organization")) as HTMLSelectElement;
    await userEvent.selectOptions(select, "o2");
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/auth/switch-org", { organization_id: "o2" }),
    );
  });

  it("does not call switch-org when re-selecting the already-active org", async () => {
    renderSwitcher();
    const select = (await screen.findByLabelText("Switch organization")) as HTMLSelectElement;
    await userEvent.selectOptions(select, me.active_org!.id);
    expect(postMock).not.toHaveBeenCalled();
  });

  it("offers super-admin act-as targets not already in me.orgs", async () => {
    me = meFixture("admin", {
      is_super_admin: true,
      orgs: [{ id: "o1", name: "Acme Care", slug: "acme-care", role: "admin" }],
      active_org: { id: "o1", name: "Acme Care", slug: "acme-care", role: "admin" },
    });
    organizations = [
      { id: "o1", name: "Acme Care", slug: "acme-care", status: "active" },
      { id: "o9", name: "Zed Org", slug: "zed-org", status: "active" },
    ];
    renderSwitcher();
    await waitFor(() => expect(getMock).toHaveBeenCalledWith("/v1/admin/organizations"));
    await screen.findByRole("option", { name: /Zed Org/ });
    // The act-as target appears exactly once (not duplicated with the membership).
    expect(screen.getAllByRole("option", { name: "Acme Care" })).toHaveLength(1);
  });

  it("does not query organizations for a non-super-admin", async () => {
    renderSwitcher();
    await screen.findByLabelText("Switch organization");
    expect(getMock).not.toHaveBeenCalledWith("/v1/admin/organizations");
  });

  it("surfaces a switch-org failure as a toast", async () => {
    const { ApiError } = await import("../lib/api");
    me = meFixture("admin", {
      orgs: [
        { id: "o1", name: "Acme Care", slug: "acme-care", role: "admin" },
        { id: "o2", name: "Beta Health", slug: "beta-health", role: "viewer" },
      ],
      active_org: { id: "o1", name: "Acme Care", slug: "acme-care", role: "admin" },
    });
    postMock.mockRejectedValue(new ApiError(403, "not a member"));
    renderSwitcher();
    const select = (await screen.findByLabelText("Switch organization")) as HTMLSelectElement;
    await userEvent.selectOptions(select, "o2");
    await waitFor(() => expect(pushToastMock).toHaveBeenCalledWith("not a member", undefined));
  });
});
