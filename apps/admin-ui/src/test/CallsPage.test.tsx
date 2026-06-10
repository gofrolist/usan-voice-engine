// apps/admin-ui/src/test/CallsPage.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { AdminCallSummary } from "../types/api";

const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (u: string) => getMock(u) },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));

import { CallsPage } from "../features/calls/CallsPage";

let seq = 0;
function call(over: Partial<AdminCallSummary> = {}): AdminCallSummary {
  seq += 1;
  return {
    id: `00000000-0000-0000-0000-${String(seq).padStart(12, "0")}`,
    elder_id: "11111111-1111-1111-1111-111111111111",
    elder_name: `Elder ${seq}`,
    masked_phone: "***4567",
    direction: "outbound",
    status: "completed",
    origin: null,
    attempt: 1,
    started_at: "2026-06-09T10:00:00Z",
    ended_at: "2026-06-09T10:05:00Z",
    duration_seconds: 300,
    end_reason: "agent_hangup",
    has_recording: false,
    created_at: "2026-06-09T09:59:00Z",
    ...over,
  };
}

function rows(n: number): AdminCallSummary[] {
  return Array.from({ length: n }, () => call());
}

function lastUrl(): string {
  return getMock.mock.calls.at(-1)?.[0] as string;
}

function renderPage(initialEntry = "/calls") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/calls" element={<CallsPage />} />
          <Route path="/calls/:id" element={<div>DETAIL</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  seq = 0;
});
afterEach(() => vi.clearAllMocks());

describe("CallsPage", () => {
  it("renders masked phone, elder name and origin badges", async () => {
    getMock.mockResolvedValue([
      call({
        elder_name: "Edna Moore",
        origin: { source: "schedule", id: "s1", ordinal: "2026-06-09" },
      }),
      call({ origin: { source: "batch", id: "b1", ordinal: 3 } }),
      call({ direction: "inbound", origin: null }),
      call({ direction: "outbound", origin: null }),
    ]);
    renderPage();

    // Scope to the table: the filter bar also contains Schedule/Batch/Ad hoc options.
    const table = await screen.findByRole("table");
    expect(within(table).getByText("Schedule")).toBeInTheDocument();
    expect(within(table).getByText("Batch")).toBeInTheDocument();
    // Inbound call with no origin key → "Inbound" badge, not "Ad hoc".
    expect(within(table).getByText("Inbound")).toBeInTheDocument();
    expect(within(table).getByText("Ad hoc")).toBeInTheDocument();
    expect(within(table).getByText("Edna Moore")).toBeInTheDocument();
    // The payload only ever carries masked_phone — assert the masked text renders.
    expect(within(table).getAllByText("***4567")).toHaveLength(4);
  });

  it("filter change resets offset to 0", async () => {
    const user = userEvent.setup();
    getMock.mockImplementation(() => Promise.resolve(rows(50)));
    renderPage();
    await screen.findByRole("table");

    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(lastUrl()).toContain("offset=50"));

    await user.selectOptions(screen.getByLabelText("Status"), "completed");
    await waitFor(() => {
      expect(lastUrl()).toContain("status=completed");
      expect(lastUrl()).toContain("offset=0");
    });
  });

  it("To date is sent exclusive (+1 day) and labeled inclusive", async () => {
    getMock.mockResolvedValue(rows(3));
    renderPage();
    await screen.findByRole("table");

    // getByLabelText doubles as the label-copy assertion: the field must read
    // "To (inclusive)" while the wire value is the day after (exclusive bound).
    const to = screen.getByLabelText("To (inclusive)");
    fireEvent.change(to, { target: { value: "2026-06-10" } });
    await waitFor(() => expect(lastUrl()).toContain("created_to=2026-06-11"));
  });

  it("honors elder_id from the URL", async () => {
    getMock.mockResolvedValue(rows(1));
    renderPage("/calls?elder_id=7f0e8b9a-1111-2222-3333-444455556666");

    await waitFor(() =>
      expect(lastUrl()).toContain("elder_id=7f0e8b9a-1111-2222-3333-444455556666"),
    );
  });

  it("hasNext heuristic: full page enables Next, short page disables it", async () => {
    getMock.mockResolvedValue(rows(50));
    const first = renderPage();
    await screen.findByRole("table");

    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(screen.getByText("1–50")).toBeInTheDocument();
    first.unmount();

    getMock.mockResolvedValue(rows(3));
    renderPage();
    await screen.findByRole("table");
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    expect(screen.getByText("1–3")).toBeInTheDocument();
  });

  it("row click navigates to detail", async () => {
    const user = userEvent.setup();
    getMock.mockResolvedValue([call({ elder_name: "Edna Moore" })]);
    renderPage();

    await user.click(await screen.findByText("Edna Moore"));
    expect(await screen.findByText("DETAIL")).toBeInTheDocument();
  });

  it("shows a spinner while loading", () => {
    getMock.mockReturnValue(new Promise(() => {}));
    renderPage();

    expect(screen.getByText(/Loading calls/)).toBeInTheDocument();
  });

  it("shows a red error message when the fetch fails", async () => {
    const { ApiError } = await import("../lib/api");
    getMock.mockRejectedValue(new ApiError(500, "boom"));
    renderPage();

    expect(await screen.findByText(/Failed to load calls: boom/)).toBeInTheDocument();
  });

  it("shows the empty state when no calls match", async () => {
    getMock.mockResolvedValue([]);
    renderPage();

    expect(await screen.findByText("No calls match these filters")).toBeInTheDocument();
  });
});
