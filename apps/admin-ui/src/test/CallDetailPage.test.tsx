// apps/admin-ui/src/test/CallDetailPage.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { fmtDate } from "../lib/format";
import type { AdminCallDetail } from "../types/api";

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

import { CallDetailPage } from "../features/calls/CallDetailPage";

const CALL_ID = "00000000-0000-0000-0000-000000000001";
const PARENT_ID = "00000000-0000-0000-0000-000000000099";
const CONTACT_ID = "11111111-1111-1111-1111-111111111111";

function detail(over: Partial<AdminCallDetail> = {}): AdminCallDetail {
  return {
    id: CALL_ID,
    contact_id: CONTACT_ID,
    contact_name: "Edna Moore",
    masked_phone: "***4567",
    direction: "outbound",
    status: "completed",
    origin: { source: "schedule", id: "s1", ordinal: "2026-06-09" },
    attempt: 1,
    started_at: "2026-06-09T10:00:02Z",
    ended_at: "2026-06-09T10:05:00Z",
    duration_seconds: 298,
    end_reason: "agent_hangup",
    has_recording: false,
    created_at: "2026-06-09T09:59:00Z",
    livekit_room: "room-1",
    parent_call_id: null,
    scheduled_at: null,
    answered_at: "2026-06-09T10:00:05Z",
    recording_status: null,
    presigned_recording_url: null,
    recording_url_ttl_s: null,
    transcript: [],
    ...over,
  };
}

// Route-by-URL mock (QueuesPage.test.tsx pattern): the page must hit this
// call's admin detail endpoint — any other GET is a bug, not a silent success.
function respondWith(d: AdminCallDetail) {
  getMock.mockImplementation((url: string) =>
    url === `/v1/admin/calls/${CALL_ID}`
      ? Promise.resolve(d)
      : Promise.reject(new Error(`unexpected GET ${url}`)),
  );
}

function renderPage(id = CALL_ID) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[`/calls/${id}`]}>
        <Routes>
          <Route path="/calls/:id" element={<CallDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

// Braces matter: `() => getMock.mockReset()` would RETURN the mock (mockReset returns
// it), and vitest calls a function returned from a hook as teardown — invoking
// getMock() after each test and leaking an unhandled rejected/pending promise.
beforeEach(() => {
  getMock.mockReset();
});
afterEach(() => vi.clearAllMocks());

describe("CallDetailPage", () => {
  it("shows a spinner while loading", () => {
    getMock.mockReturnValue(new Promise(() => {}));
    renderPage();

    expect(screen.getByText(/Loading call/)).toBeInTheDocument();
  });

  it("shows a distinct not-found state on 404 (stale queue links happen)", async () => {
    const { ApiError } = await import("../lib/api");
    getMock.mockRejectedValue(new ApiError(404, "call not found"));
    renderPage();

    expect(await screen.findByText(/Call not found/)).toBeInTheDocument();
  });

  it("shows a red error block with the detail on other errors", async () => {
    const { ApiError } = await import("../lib/api");
    getMock.mockRejectedValue(new ApiError(500, "boom"));
    renderPage();

    expect(await screen.findByText(/boom/)).toBeInTheDocument();
    expect(screen.queryByText(/Call not found/)).not.toBeInTheDocument();
  });

  it("renders the header card: contact link, masked phone, call facts", async () => {
    respondWith(detail());
    renderPage();

    // Contact name links to this contact's filtered calls list.
    const contactLink = await screen.findByRole("link", { name: "Edna Moore" });
    expect(contactLink).toHaveAttribute("href", `/calls?contact_id=${CONTACT_ID}`);
    expect(screen.getByText("***4567")).toBeInTheDocument();

    expect(screen.getByText("outbound")).toBeInTheDocument();
    expect(screen.getByText("completed")).toBeInTheDocument();
    expect(screen.getByText("Schedule")).toBeInTheDocument();
    expect(screen.getByText(fmtDate("2026-06-09T09:59:00Z"))).toBeInTheDocument(); // created
    expect(screen.getByText(fmtDate("2026-06-09T10:00:02Z"))).toBeInTheDocument(); // started
    expect(screen.getByText(fmtDate("2026-06-09T10:00:05Z"))).toBeInTheDocument(); // answered
    expect(screen.getByText(fmtDate("2026-06-09T10:05:00Z"))).toBeInTheDocument(); // ended
    expect(screen.getByText("4:58")).toBeInTheDocument(); // duration
    expect(screen.getByText("agent_hangup")).toBeInTheDocument(); // end reason
  });

  it("links a retry child to its parent call as 'attempt N — view parent'", async () => {
    respondWith(detail({ attempt: 2, parent_call_id: PARENT_ID }));
    renderPage();

    const parentLink = await screen.findByRole("link", { name: "attempt 2 — view parent" });
    expect(parentLink).toHaveAttribute("href", `/calls/${PARENT_ID}`);
  });

  it("renders transcript segments from the detail payload", async () => {
    respondWith(
      detail({
        transcript: [
          {
            role: "assistant",
            content: "Good morning, Edna. How are you feeling today?",
            tool_name: null,
            tool_args: null,
            started_at: "2026-06-09T10:00:05Z",
            ended_at: "2026-06-09T10:00:09Z",
          },
        ],
      }),
    );
    renderPage();

    const segment = await screen.findByText("Good morning, Edna. How are you feeling today?");
    expect(segment.closest("[data-role]")).toHaveAttribute("data-role", "assistant");
  });
});
