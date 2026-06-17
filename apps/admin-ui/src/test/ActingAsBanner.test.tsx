import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { Me } from "../types/api";
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

import { ActingAsBanner } from "../components/ActingAsBanner";

let me: Me = meFixture("admin");

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") return Promise.resolve(me);
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function renderBanner() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ActingAsBanner />
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
});
afterEach(() => vi.clearAllMocks());

describe("ActingAsBanner", () => {
  it("renders nothing when not acting-as", async () => {
    const { container } = renderBanner();
    // Resolve the session so any conditional render has its data.
    await waitFor(() => expect(getMock).toHaveBeenCalledWith("/v1/auth/me"));
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the acting-as org name when acting_as is true", async () => {
    me = meFixture("admin", {
      is_super_admin: true,
      acting_as: true,
      active_org: { id: "o2", name: "Beta Health", slug: "beta-health", role: null },
      orgs: [{ id: "o1", name: "Acme Care", slug: "acme-care", role: "admin" }],
    });
    renderBanner();
    expect(await screen.findByText(/Beta Health/)).toBeInTheDocument();
  });

  it("Exit switches back to the first home org", async () => {
    me = meFixture("admin", {
      is_super_admin: true,
      acting_as: true,
      active_org: { id: "o2", name: "Beta Health", slug: "beta-health", role: null },
      orgs: [{ id: "o1", name: "Acme Care", slug: "acme-care", role: "admin" }],
    });
    postMock.mockResolvedValue({ ...me, orgs: [] });
    renderBanner();
    const exit = await screen.findByRole("button", { name: /exit/i });
    await userEvent.click(exit);
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/auth/switch-org", { organization_id: "o1" }),
    );
  });

  it("Exit links to the org console when the caller has no home org", async () => {
    me = meFixture("admin", {
      is_super_admin: true,
      acting_as: true,
      active_org: { id: "o2", name: "Beta Health", slug: "beta-health", role: null },
      orgs: [],
    });
    renderBanner();
    const exit = await screen.findByRole("link", { name: /exit/i });
    expect(exit).toHaveAttribute("href", "/organizations");
    expect(postMock).not.toHaveBeenCalled();
  });
});
