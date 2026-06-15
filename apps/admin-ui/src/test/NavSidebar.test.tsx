// apps/admin-ui/src/test/NavSidebar.test.tsx
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";

// Route-by-URL api mock — /v1/auth/me is served too, so viewer/admin gating is
// driven by the real useIsAdmin query, not a mocked hook.
const getMock = vi.fn();
const postMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string) => postMock(u),
  },
}));

import { NavSidebar } from "../components/NavSidebar";

let role: "admin" | "viewer" = "viewer";

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") {
    return Promise.resolve({ email: "me@example.com", role });
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
});

describe("NavSidebar Operate group", () => {
  it("viewer sees the Operate heading with Calls and Queues links", async () => {
    renderSidebar();
    // Wait for the session to resolve so adminOnly filtering is final.
    await screen.findByText("me@example.com");
    expect(screen.getByText("Operate")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Calls" })).toHaveAttribute("href", "/calls");
    expect(screen.getByRole("link", { name: "Queues" })).toHaveAttribute("href", "/queues");
  });

  it("regression: Contacts stays hidden for the viewer", async () => {
    renderSidebar();
    await screen.findByText("me@example.com");
    expect(screen.queryByRole("link", { name: "Contacts" })).not.toBeInTheDocument();
    // The legacy "Contacts" label is fully gone from the nav (US4 / SC-007).
    expect(screen.queryByRole("link", { name: "Contacts" })).not.toBeInTheDocument();
    // Operate links are not adminOnly — still present for the viewer.
    expect(screen.getByRole("link", { name: "Calls" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Queues" })).toBeInTheDocument();
  });

  it("regression: Contacts is visible for admin (label + /contacts href)", async () => {
    role = "admin";
    renderSidebar();
    await screen.findByText("me@example.com");
    // US4: the nav reads "Contacts" and the user-visible route token is /contacts.
    expect(screen.getByRole("link", { name: "Contacts" })).toHaveAttribute("href", "/contacts");
    expect(screen.queryByRole("link", { name: "Elders" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Calls" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Queues" })).toBeInTheDocument();
  });

  it("shows Variables under Config for a viewer (not adminOnly)", async () => {
    renderSidebar();
    await screen.findByText("me@example.com");
    expect(screen.getByText("Config")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Variables" })).toHaveAttribute(
      "href",
      "/custom-variables",
    );
  });

  it("groups render in order Build, Config, Operate, System", async () => {
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
});
