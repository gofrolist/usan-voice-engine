import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ScheduleResponse } from "../types/api";

const getMock = vi.fn();
const patchMock = vi.fn();
const delMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: vi.fn(),
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
vi.mock("../components/ui/toast", () => ({ pushToast: vi.fn() }));

import { ScheduleSection } from "../features/schedules/ScheduleSection";

function sched(over: Partial<ScheduleResponse> = {}): ScheduleResponse {
  return {
    id: "s1",
    contact_id: "c1",
    slot: "morning",
    enabled: true,
    window_start_local: "09:00:00",
    window_end_local: "11:00:00",
    days_of_week: ["monday", "tuesday"],
    dynamic_vars: {},
    profile_override: null,
    next_run_at: "2026-06-24T13:00:00Z",
    last_materialized_date: null,
    last_result: null,
    last_result_at: null,
    created_at: "",
    updated_at: "",
    ...over,
  };
}

let schedules: ScheduleResponse[] = [];
function renderSection() {
  getMock.mockImplementation((u: string) => {
    if (u.startsWith("/v1/admin/schedules")) return Promise.resolve(schedules);
    if (u.startsWith("/v1/admin/profiles")) return Promise.resolve([]);
    return Promise.reject(new Error(u));
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ScheduleSection contactId="c1" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  patchMock.mockReset();
  delMock.mockReset();
  schedules = [sched()];
});
afterEach(() => vi.clearAllMocks());

describe("ScheduleSection", () => {
  it("lists existing schedules and offers Add for the free slot", async () => {
    renderSection();
    expect(await screen.findByText(/morning/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Add schedule/i })).toBeInTheDocument();
  });

  it("toggles enabled via PATCH", async () => {
    const user = userEvent.setup();
    patchMock.mockResolvedValue(sched({ enabled: false }));
    renderSection();
    await screen.findByText(/morning/i);
    await user.click(screen.getByRole("button", { name: /Disable/i }));
    await waitFor(() =>
      expect(patchMock).toHaveBeenCalledWith("/v1/admin/schedules/s1", { enabled: false }),
    );
  });

  it("hides Add schedule when both slots are taken", async () => {
    schedules = [sched({ slot: "morning" }), sched({ id: "s2", slot: "evening" })];
    renderSection();
    await screen.findByText(/morning/i);
    expect(screen.queryByRole("button", { name: /Add schedule/i })).not.toBeInTheDocument();
  });

  it("deletes a schedule after confirmation", async () => {
    const user = userEvent.setup();
    delMock.mockResolvedValue(undefined);
    renderSection();
    await screen.findByText(/morning/i);
    await user.click(screen.getByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(delMock).toHaveBeenCalledWith("/v1/admin/schedules/s1"));
  });
});
