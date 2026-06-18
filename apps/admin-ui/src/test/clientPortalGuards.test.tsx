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
