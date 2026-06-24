import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ScheduleResponse } from "../types/api";
import { meFixture } from "./meFixture";

const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (u: string) => getMock(u), patch: vi.fn(), del: vi.fn() },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));
vi.mock("../components/ui/toast", () => ({ pushToast: vi.fn() }));

import { SchedulesPage } from "../features/schedules/SchedulesPage";

let lastUrl = "";
const row: ScheduleResponse = {
  id: "s1",
  contact_id: "c1",
  contact_name: "Edna Moore",
  slot: "morning",
  enabled: true,
  window_start_local: "09:00:00",
  window_end_local: "11:00:00",
  days_of_week: ["monday"],
  dynamic_vars: {},
  profile_override: null,
  next_run_at: "2026-06-24T13:00:00Z",
  last_materialized_date: null,
  last_result: "skipped_window",
  last_result_at: null,
  created_at: "",
  updated_at: "",
};

function renderPage() {
  getMock.mockImplementation((u: string) => {
    if (u === "/v1/auth/me") return Promise.resolve(meFixture("admin"));
    if (u.startsWith("/v1/admin/schedules")) {
      lastUrl = u;
      return Promise.resolve([row]);
    }
    if (u.startsWith("/v1/admin/profiles")) return Promise.resolve([]);
    return Promise.reject(new Error(u));
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SchedulesPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  lastUrl = "";
});
afterEach(() => vi.clearAllMocks());

describe("SchedulesPage", () => {
  it("links each row to the contact by name and shows last_result", async () => {
    renderPage();
    const link = await screen.findByRole("link", { name: /Edna Moore/ });
    expect(link).toHaveAttribute("href", "/contacts/c1");
    expect(screen.getByText("skipped_window")).toBeInTheDocument();
  });

  it("falls back to the contact UUID when the name is null", async () => {
    getMock.mockImplementation((u: string) => {
      if (u === "/v1/auth/me") return Promise.resolve(meFixture("admin"));
      if (u.startsWith("/v1/admin/schedules")) return Promise.resolve([{ ...row, contact_name: null }]);
      if (u.startsWith("/v1/admin/profiles")) return Promise.resolve([]);
      return Promise.reject(new Error(u));
    });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <SchedulesPage />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    const link = await screen.findByRole("link", { name: /c1/ });
    expect(link).toHaveAttribute("href", "/contacts/c1");
  });

  it("applies the 'who missed' filter as last_result=skipped_window", async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("skipped_window");
    await user.click(screen.getByRole("button", { name: /Who missed/i }));
    await waitFor(() => expect(lastUrl).toContain("last_result=skipped_window"));
  });
});
