import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { Me, VersionSummary } from "../types/api";

// Mock the api module: getMock serves the session/version-list reads, postMock is the
// rollback mutation (the behavior under test).
const getMock = vi.fn();
const postMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b?: unknown) => postMock(u, b),
    put: vi.fn(),
    del: vi.fn(),
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

// Observe toasts (the QueuesPage.test.tsx pattern): rollback has no form to land
// field errors on, so the parsed 422 violation message must surface via toast.
const pushToastMock = vi.fn();
vi.mock("../components/ui/toast", () => ({
  pushToast: (message: string, tone?: string) => pushToastMock(message, tone),
}));

import { VersionHistoryPage } from "../features/versions/VersionHistoryPage";

// Same custom-PHI SMS violation shape the save/publish paths get (the shared
// server-side helper fabricates the loc; spec §6.3 routes all three through the
// same field-error parsing).
const PHI_VIOLATION_MSG =
  "SMS template 'followup' body references protected health information " +
  "({{diagnosis}}); SMS bodies may use non-PHI variables only";
const PHI_DETAIL = JSON.stringify([
  {
    loc: ["body", "config", "tools", "sms", "templates", 0, "body"],
    msg: PHI_VIOLATION_MSG,
    type: "value_error.custom_phi_sms",
  },
]);

function versions(): VersionSummary[] {
  return [
    {
      version: 1,
      note: null,
      published_by: "ops@example.com",
      published_at: "2026-06-01T00:00:00Z",
    },
  ];
}

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") {
    return Promise.resolve({ email: "me@example.com", role: "admin" } satisfies Me);
  }
  if (url === "/v1/admin/profiles/p1/versions") return Promise.resolve(versions());
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/profiles/p1/versions"]}>
        <Routes>
          <Route path="/profiles/:id/versions" element={<VersionHistoryPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("VersionHistoryPage rollback 422 routing", () => {
  beforeEach(() => {
    getMock.mockReset();
    postMock.mockReset();
    pushToastMock.mockReset();
    getMock.mockImplementation(routeGet);
  });
  afterEach(() => vi.clearAllMocks());

  it("rollback 422 surfaces the violation message", async () => {
    const { ApiError } = await import("../lib/api");
    postMock.mockRejectedValue(new ApiError(422, PHI_DETAIL));
    renderPage();
    const user = userEvent.setup();

    // The Roll back action is admin-gated; it appearing proves the mocked session won.
    await user.click(await screen.findByRole("button", { name: "Roll back" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Roll back" }));

    // Rollback posts with no body; the mock wrapper forwards the undefined.
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/profiles/p1/rollback/1", undefined),
    );
    // EXACTLY one toast carrying the parsed violation `msg` — never the raw JSON
    // detail blob, never swallowed.
    await waitFor(() => expect(pushToastMock).toHaveBeenCalledTimes(1));
    expect(pushToastMock.mock.calls[0]?.[0]).toBe(PHI_VIOLATION_MSG);
  });
});
