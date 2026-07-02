import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { KbDetail } from "../types/api";
import { meFixture } from "./meFixture";

const getMock = vi.fn();
const postMock = vi.fn();
const delMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b: unknown) => postMock(u, b),
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

import { KnowledgeBaseDetailPage } from "../features/knowledgeBases/KnowledgeBaseDetailPage";

const KB_ID = "aaaa1111-1111-1111-1111-111111111111";
const detail: KbDetail = {
  id: KB_ID,
  name: "Wellness FAQ",
  status: "complete",
  error_detail: null,
  sources: [
    {
      id: "cccc3333-3333-3333-3333-333333333333",
      title: "Intro",
      status: "embedded",
      created_at: "2026-07-02T10:00:00Z",
    },
  ],
  created_at: "2026-07-02T09:00:00Z",
  updated_at: "2026-07-02T10:00:00Z",
};

function renderDetail(role: "admin" | "viewer") {
  getMock.mockImplementation((url: string) => {
    if (url === "/v1/auth/me") return Promise.resolve(meFixture(role));
    if (url === `/v1/admin/knowledge-bases/${KB_ID}`) return Promise.resolve(detail);
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[`/knowledge-bases/${KB_ID}`]}>
        <Routes>
          <Route path="/knowledge-bases/:id" element={<KnowledgeBaseDetailPage />} />
          <Route path="/knowledge-bases" element={<div>list</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("KnowledgeBaseDetailPage", () => {
  it("renders the KB and its sources", async () => {
    renderDetail("admin");
    expect(await screen.findByText("Wellness FAQ")).toBeInTheDocument();
    expect(screen.getByText("Intro")).toBeInTheDocument();
  });

  it("adds a text source", async () => {
    postMock.mockResolvedValue({ ...detail, status: "in_progress" });
    renderDetail("admin");
    await screen.findByText("Wellness FAQ");
    await userEvent.type(screen.getByLabelText("Title"), "New doc");
    await userEvent.type(screen.getByLabelText("Text"), "some content");
    await userEvent.click(screen.getByRole("button", { name: "Add source" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith(`/v1/admin/knowledge-bases/${KB_ID}/sources`, {
        title: "New doc",
        text: "some content",
      }),
    );
  });

  it("hides write controls from viewers", async () => {
    renderDetail("viewer");
    await screen.findByText("Wellness FAQ");
    expect(screen.queryByRole("button", { name: "Add source" })).toBeNull();
  });
});
