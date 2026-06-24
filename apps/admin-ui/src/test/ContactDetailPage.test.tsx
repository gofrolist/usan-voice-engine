import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ContactDetail } from "../types/api";
import { meFixture } from "./meFixture";

const getMock = vi.fn();
const delMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: vi.fn(),
    patch: vi.fn(),
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

import { ContactDetailPage } from "../features/contacts/ContactDetailPage";

const detail: ContactDetail = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "Edna Moore",
  masked_phone: "***4567",
  timezone: "America/New_York",
  agent_profile_id: null,
  agent_profile_name: null,
  external_id: "EHR-9",
  preferred_voice: null,
  metadata: {},
  created_at: "2026-06-20T09:00:00Z",
  updated_at: "2026-06-20T09:00:00Z",
};

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") return Promise.resolve(meFixture("admin"));
  if (url === `/v1/admin/contacts/${detail.id}`) return Promise.resolve(detail);
  if (url.startsWith("/v1/admin/schedules")) return Promise.resolve([]);
  if (url.startsWith("/v1/admin/profiles")) return Promise.resolve([]);
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function renderAt(id: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/contacts/${id}`]}>
        <Routes>
          <Route path="/contacts/:id" element={<ContactDetailPage />} />
          <Route path="/contacts" element={<div>contacts list</div>} />
          <Route path="/calls" element={<div>calls page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  delMock.mockReset();
  getMock.mockImplementation(routeGet);
});
afterEach(() => vi.clearAllMocks());

describe("ContactDetailPage", () => {
  it("renders the contact header with masked phone", async () => {
    renderAt(detail.id);
    expect(await screen.findByText("Edna Moore")).toBeInTheDocument();
    expect(screen.getByText("***4567")).toBeInTheDocument();
  });

  it("delete asks for confirmation, then DELETEs", async () => {
    const user = userEvent.setup();
    delMock.mockResolvedValue(undefined);
    renderAt(detail.id);
    await screen.findByText("Edna Moore");
    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(delMock).not.toHaveBeenCalled(); // confirm first
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => expect(delMock).toHaveBeenCalledWith(`/v1/admin/contacts/${detail.id}`));
  });

  it("shows a not-found state for a missing contact", async () => {
    const { ApiError } = await import("../lib/api");
    getMock.mockImplementation((url: string) => {
      if (url === "/v1/auth/me") return Promise.resolve(meFixture("admin"));
      if (url.startsWith("/v1/admin/contacts/"))
        return Promise.reject(new ApiError(404, "not found"));
      if (url.startsWith("/v1/admin/schedules")) return Promise.resolve([]);
      return Promise.reject(new Error(`unexpected GET ${url}`));
    });
    renderAt("00000000-0000-0000-0000-000000000000");
    expect(await screen.findByText(/Contact not found/)).toBeInTheDocument();
  });
});
