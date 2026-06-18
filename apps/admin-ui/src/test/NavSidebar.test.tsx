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
import { meFixture } from "./meFixture";

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

  it("hides Audit for a viewer (P4 ADMIN-gated)", async () => {
    role = "viewer";
    renderSidebar();
    await screen.findByText("me@example.com");
    expect(screen.queryByRole("link", { name: "Audit" })).not.toBeInTheDocument();
  });

  it("shows Audit for a client admin (P4 ADMIN-gated)", async () => {
    role = "admin";
    renderSidebar();
    await screen.findByText("me@example.com");
    expect(screen.getByRole("link", { name: "Audit" })).toHaveAttribute("href", "/audit");
  });

  it("renders a decorative icon inside every nav link without changing link names", async () => {
    role = "admin"; // widest set of links (adminOnly Contacts/Members visible)
    const { container } = renderSidebar();
    await screen.findByText("me@example.com");
    const links = container.querySelectorAll("nav a");
    expect(links.length).toBeGreaterThan(0);
    links.forEach((link) => {
      const svg = link.querySelector("svg");
      expect(svg).not.toBeNull();
      // Icons must be aria-hidden so the link's accessible name stays its text label
      // (keeps every getByRole("link", { name }) query above matching).
      expect(svg).toHaveAttribute("aria-hidden", "true");
    });
    // Sanity: a representative link still resolves by its text name.
    expect(screen.getByRole("link", { name: "Calls" })).toBeInTheDocument();
  });

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
});
