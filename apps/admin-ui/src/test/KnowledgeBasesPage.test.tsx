import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { KbSummary } from "../types/api";
import { meFixture } from "./meFixture";

const getMock = vi.fn();
const postMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b: unknown) => postMock(u, b),
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
vi.mock("../components/ui/toast", () => ({ pushToast: vi.fn() }));

import { KnowledgeBasesPage } from "../features/knowledgeBases/KnowledgeBasesPage";

const rows: KbSummary[] = [
  {
    id: "aaaa1111-1111-1111-1111-111111111111",
    agent_ref: "knowledge_base_aaaa1111111111111111111111111111",
    name: "Wellness FAQ",
    status: "complete",
    source_count: 2,
    updated_at: "2026-07-02T10:00:00Z",
  },
];

function renderPage(role: "admin" | "viewer") {
  getMock.mockImplementation((url: string) => {
    if (url === "/v1/auth/me") return Promise.resolve(meFixture(role));
    if (url === "/v1/admin/knowledge-bases") return Promise.resolve(rows);
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={["/knowledge-bases"]}>
        <Routes>
          <Route path="/knowledge-bases" element={<KnowledgeBasesPage />} />
          <Route path="/knowledge-bases/:id" element={<div>detail</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("KnowledgeBasesPage", () => {
  it("lists knowledge bases with a status badge", async () => {
    renderPage("admin");
    expect(await screen.findByText("Wellness FAQ")).toBeInTheDocument();
    expect(screen.getByText("complete")).toBeInTheDocument();
  });

  it("shows the New button to admins and creates a KB", async () => {
    postMock.mockResolvedValue({
      id: "bbbb2222-2222-2222-2222-222222222222",
      name: "New KB",
      status: "in_progress",
      error_detail: null,
      sources: [],
      created_at: "2026-07-02T10:00:00Z",
      updated_at: "2026-07-02T10:00:00Z",
    });
    renderPage("admin");
    await screen.findByText("Wellness FAQ");
    await userEvent.click(screen.getByRole("button", { name: "New knowledge base" }));
    await userEvent.type(screen.getByLabelText("Name"), "New KB");
    await userEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/knowledge-bases", { name: "New KB" }),
    );
  });

  it("hides the New button from viewers", async () => {
    renderPage("viewer");
    await screen.findByText("Wellness FAQ");
    expect(screen.queryByRole("button", { name: "New knowledge base" })).toBeNull();
  });
});
