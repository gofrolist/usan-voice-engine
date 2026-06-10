// apps/admin-ui/src/test/QueuesPage.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { focusManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import type { CallbackRequestSummary, FollowupFlagSummary, QueuesSummary } from "../types/api";

// Route-by-URL api mock — /v1/auth/me is served too, so viewer/admin gating is
// driven by the real useIsAdmin query, not a mocked hook.
const getMock = vi.fn();
const patchMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    patch: (u: string, b?: unknown) => patchMock(u, b),
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

import { QueuesPage } from "../features/queues/QueuesPage";
import { useFollowUpFlags } from "../features/queues/hooks";

let role: "admin" | "viewer" = "admin";
let flags: FollowupFlagSummary[] = [];
let callbacks: CallbackRequestSummary[] = [];
let summary: QueuesSummary = {
  flags_open: 2,
  flags_open_urgent: 1,
  flags_acknowledged: 0,
  callbacks_open: 3,
  callbacks_acknowledged: 0,
};

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") {
    return Promise.resolve({ email: "me@example.com", role });
  }
  if (url === "/v1/admin/queues/summary") return Promise.resolve(summary);
  if (url.startsWith("/v1/admin/follow-up-flags?")) return Promise.resolve(flags);
  if (url.startsWith("/v1/admin/callback-requests?")) return Promise.resolve(callbacks);
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function flagRow(over: Partial<FollowupFlagSummary> = {}): FollowupFlagSummary {
  return {
    id: 1,
    call_id: "00000000-0000-0000-0000-000000000001",
    elder_id: "11111111-1111-1111-1111-111111111111",
    elder_name: "Edna Moore",
    masked_phone: "***4567",
    severity: "routine",
    category: "medical",
    reason: "dizzy spells",
    status: "open",
    status_updated_at: null,
    status_updated_by: null,
    created_at: "2026-06-09T09:00:00Z",
    ...over,
  };
}

function cbRow(over: Partial<CallbackRequestSummary> = {}): CallbackRequestSummary {
  return {
    id: 1,
    call_id: "00000000-0000-0000-0000-000000000002",
    elder_id: "11111111-1111-1111-1111-111111111111",
    elder_name: "Edna Moore",
    masked_phone: "***4567",
    requested_time_text: "tomorrow after lunch",
    requested_at: null,
    notes: null,
    status: "open",
    status_updated_at: null,
    status_updated_by: null,
    created_at: "2026-06-09T09:00:00Z",
    ...over,
  };
}

function flagsUrls(): string[] {
  return getMock.mock.calls
    .map((c) => c[0] as string)
    .filter((u) => u.startsWith("/v1/admin/follow-up-flags?"));
}

function callbacksUrls(): string[] {
  return getMock.mock.calls
    .map((c) => c[0] as string)
    .filter((u) => u.startsWith("/v1/admin/callback-requests?"));
}

// Probe so tests can assert the page wrote its state back into the URL.
function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location-search">{location.search}</div>;
}

function renderPage(initialEntry = "/queues") {
  const client = new QueryClient({
    // Mirror the app's global default (lib/queryClient.ts): focus-refetch is OFF
    // unless a query opts in.
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route
            path="/queues"
            element={
              <>
                <QueuesPage />
                <LocationProbe />
              </>
            }
          />
          <Route path="/calls" element={<div>CALLS LIST</div>} />
          <Route path="/calls/:id" element={<div>CALL DETAIL</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  patchMock.mockReset();
  pushToastMock.mockReset();
  getMock.mockImplementation(routeGet);
  role = "admin";
  flags = [];
  callbacks = [];
  summary = {
    flags_open: 2,
    flags_open_urgent: 1,
    flags_acknowledged: 0,
    callbacks_open: 3,
    callbacks_acknowledged: 0,
  };
});
afterEach(() => vi.clearAllMocks());

describe("QueuesPage", () => {
  it("tab, status and offset sync to the URL", async () => {
    const user = userEvent.setup();
    callbacks = [cbRow()];
    renderPage("/queues?tab=callbacks&status=resolved&offset=50");

    await waitFor(() => {
      const url = callbacksUrls().at(-1);
      expect(url).toContain("status=resolved");
      expect(url).toContain("offset=50");
    });

    await user.click(screen.getByRole("tab", { name: /Follow-up flags/ }));
    await waitFor(() => {
      expect(flagsUrls().length).toBeGreaterThan(0);
      expect(screen.getByTestId("location-search").textContent).toContain("tab=flags");
    });
  });

  it("tab labels carry summary counts", async () => {
    renderPage();

    expect(
      await screen.findByRole("tab", { name: "Follow-up flags (2 open, 1 urgent)" }),
    ).toBeInTheDocument();
    expect(await screen.findByRole("tab", { name: "Callbacks (3 open)" })).toBeInTheDocument();
  });

  it("default status filter is Open", async () => {
    renderPage();

    await waitFor(() => expect(flagsUrls()[0]).toContain("status=open"));
  });

  it("urgent rows are semantically marked", async () => {
    flags = [
      flagRow({ id: 1, severity: "urgent", reason: "chest pain" }),
      flagRow({ id: 2, severity: "routine", reason: "dizzy spells" }),
    ];
    renderPage();

    // Semantic markers only — never the Tailwind border/fill classes. The red
    // left-border + filled-badge styling hangs off data-severity in the
    // implementation; a restyle must not break this test.
    const urgentRow = (await screen.findByText("chest pain")).closest("tr")!;
    expect(urgentRow).toHaveAttribute("data-severity", "urgent");
    expect(within(urgentRow).getByText("urgent")).toBeInTheDocument();

    const routineRow = screen.getByText("dizzy spells").closest("tr")!;
    expect(routineRow).toHaveAttribute("data-severity", "routine");
    expect(within(routineRow).getByText("routine")).toBeInTheDocument();
  });

  it("viewer sees no actions; admin sees them", async () => {
    flags = [
      flagRow({ id: 1, status: "open", reason: "chest pain" }),
      flagRow({ id: 2, status: "acknowledged", reason: "dizzy spells" }),
    ];

    role = "viewer";
    const viewer = renderPage();
    await screen.findByText("chest pain");
    // Hidden, not disabled: viewers get no mutation affordances at all.
    expect(screen.queryByRole("button", { name: "Acknowledge" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Resolve" })).toBeNull();
    viewer.unmount();

    role = "admin";
    renderPage();
    const openRow = (await screen.findByText("chest pain")).closest("tr")!;
    await waitFor(() =>
      expect(within(openRow).getByRole("button", { name: "Acknowledge" })).toBeInTheDocument(),
    );
    expect(within(openRow).getByRole("button", { name: "Resolve" })).toBeInTheDocument();

    const ackRow = screen.getByText("dizzy spells").closest("tr")!;
    expect(within(ackRow).queryByRole("button", { name: "Acknowledge" })).toBeNull();
    expect(within(ackRow).getByRole("button", { name: "Resolve" })).toBeInTheDocument();
  });

  it("acknowledge calls api.patch and disables while pending", async () => {
    const user = userEvent.setup();
    flags = [flagRow({ id: 1, status: "open" })];
    let resolvePatch!: (v: unknown) => void;
    patchMock.mockReturnValue(
      new Promise((res) => {
        resolvePatch = res;
      }),
    );
    renderPage();

    const ack = await screen.findByRole("button", { name: "Acknowledge" });
    await user.click(ack);

    expect(patchMock).toHaveBeenCalledWith("/v1/admin/follow-up-flags/1", {
      status: "acknowledged",
    });
    // Double-click guard while the promise is unresolved (the server is also idempotent).
    await waitFor(() => expect(ack).toBeDisabled());

    resolvePatch(flagRow({ id: 1, status: "acknowledged" }));
  });

  it("resolve goes through ConfirmDialog", async () => {
    const user = userEvent.setup();
    flags = [flagRow({ id: 1, status: "open" })];
    patchMock.mockResolvedValue(flagRow({ id: 1, status: "resolved" }));
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Resolve" }));

    // Resolution is one-way — nothing is sent until the dialog confirms.
    const dialog = await screen.findByRole("dialog");
    expect(patchMock).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Resolve" }));
    await waitFor(() =>
      expect(patchMock).toHaveBeenCalledWith("/v1/admin/follow-up-flags/1", {
        status: "resolved",
      }),
    );
  });

  it("409 refetches and toasts", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("../lib/api");
    flags = [flagRow({ id: 1, status: "open" })];
    patchMock.mockRejectedValue(new ApiError(409, "illegal transition: resolved -> acknowledged"));
    renderPage();

    const ack = await screen.findByRole("button", { name: "Acknowledge" });
    const before = flagsUrls().length;
    await user.click(ack);

    await waitFor(() => {
      expect(pushToastMock).toHaveBeenCalledWith("Status changed elsewhere — list refreshed", "info");
      // The 409 invalidates the flags list — the GET fires again.
      expect(flagsUrls().length).toBeGreaterThan(before);
    });
  });

  it("queue hooks refetch on window focus (per-query opt-in)", async () => {
    function Harness() {
      useFollowUpFlags("open", undefined, 50, 0);
      return null;
    }
    const client = new QueryClient({
      // staleTime 0 + the app's global refetchOnWindowFocus:false default — only
      // the hook's own per-query opt-in can make the second fetch happen.
      defaultOptions: { queries: { retry: false, staleTime: 0, refetchOnWindowFocus: false } },
    });
    render(
      <QueryClientProvider client={client}>
        <Harness />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(flagsUrls()).toHaveLength(1));

    try {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
      await waitFor(() => expect(flagsUrls()).toHaveLength(2));
    } finally {
      focusManager.setFocused(undefined);
    }
  });

  it("Refresh button refetches", async () => {
    const user = userEvent.setup();
    flags = [flagRow()];
    renderPage();
    await screen.findByText("dizzy spells");

    const before = flagsUrls().length;
    await user.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(flagsUrls().length).toBeGreaterThan(before));
  });

  it("empty copy: all-clear on the default Open view, filtered copy otherwise", async () => {
    flags = [];
    const first = renderPage();
    expect(
      await screen.findByText("No open follow-up flags — all clear."),
    ).toBeInTheDocument();
    first.unmount();

    renderPage("/queues?status=resolved");
    expect(await screen.findByText("No flags match these filters")).toBeInTheDocument();
  });
});
