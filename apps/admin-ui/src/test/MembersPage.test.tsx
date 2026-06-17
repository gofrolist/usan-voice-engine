import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { Member } from "../types/api";
import { meFixture } from "./meFixture";

const getMock = vi.fn();
const postMock = vi.fn();
const patchMock = vi.fn();
const delMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b?: unknown) => postMock(u, b),
    patch: (u: string, b?: unknown) => patchMock(u, b),
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

// useIsAdmin reads /v1/auth/me; the page-level role gate keys off the fixture role.
let meRole: "admin" | "viewer" = "admin";

import { MembersPage } from "../features/members/MembersPage";

let members: Member[] = [];

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") return Promise.resolve(meFixture(meRole));
  if (url === "/v1/admin/members") return Promise.resolve(members);
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <MembersPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function member(over: Partial<Member> = {}): Member {
  return {
    email: "edna@example.com",
    role: "admin",
    added_by: "owner@example.com",
    ...over,
  };
}

describe("MembersPage", () => {
  beforeEach(() => {
    getMock.mockReset();
    postMock.mockReset();
    patchMock.mockReset();
    delMock.mockReset();
    pushToastMock.mockReset();
    getMock.mockImplementation(routeGet);
    meRole = "admin";
    members = [member()];
  });
  afterEach(() => vi.clearAllMocks());

  it("renders the member list for an admin", async () => {
    members = [member({ email: "alice@example.com", role: "viewer", added_by: null })];
    renderPage();
    expect(await screen.findByText("alice@example.com")).toBeInTheDocument();
    expect(getMock).toHaveBeenCalledWith("/v1/admin/members");
  });

  it("shows 'Admins only.' for a viewer", async () => {
    meRole = "viewer";
    renderPage();
    expect(await screen.findByText("Admins only.")).toBeInTheDocument();
  });

  it("adds a member via POST /v1/admin/members", async () => {
    members = [];
    postMock.mockResolvedValue(member({ email: "new@example.com", role: "viewer" }));
    renderPage();
    const email = await screen.findByLabelText("Email");
    await userEvent.type(email, "New@Example.com");
    await userEvent.selectOptions(screen.getByLabelText("Role"), "viewer");
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/members", {
        email: "new@example.com",
        role: "viewer",
      }),
    );
  });

  it("changes a member's role via PATCH /v1/admin/members/{email}", async () => {
    members = [member({ email: "edna@example.com", role: "admin" })];
    patchMock.mockResolvedValue(member({ email: "edna@example.com", role: "viewer" }));
    renderPage();
    const select = await screen.findByLabelText("Role for edna@example.com");
    await userEvent.selectOptions(select, "viewer");
    await waitFor(() =>
      expect(patchMock).toHaveBeenCalledWith("/v1/admin/members/edna%40example.com", {
        role: "viewer",
      }),
    );
  });

  it("removes a member via DELETE after confirming", async () => {
    members = [member({ email: "edna@example.com" })];
    delMock.mockResolvedValue(undefined);
    renderPage();
    await screen.findByText("edna@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Remove" }));
    // Confirm in the dialog.
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(delMock).toHaveBeenCalledWith("/v1/admin/members/edna%40example.com"),
    );
  });

  it("surfaces a 409 (last admin) as a toast", async () => {
    const { ApiError } = await import("../lib/api");
    members = [member({ email: "edna@example.com" })];
    delMock.mockRejectedValue(new ApiError(409, "cannot remove the last admin"));
    renderPage();
    await screen.findByText("edna@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Remove" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(pushToastMock).toHaveBeenCalledWith("cannot remove the last admin", undefined),
    );
  });
});
