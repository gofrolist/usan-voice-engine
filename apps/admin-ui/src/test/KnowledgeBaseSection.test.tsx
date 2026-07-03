import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import type { KbSummary } from "../types/api";
import type { AgentConfigForm } from "../config/agentConfigSchema";

const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (u: string) => getMock(u), post: vi.fn(), del: vi.fn() },
  ApiError: class ApiError extends Error {
    constructor(
      public status: number,
      public detail: string,
    ) {
      super(detail);
    }
  },
}));

import { KnowledgeBaseSection } from "../features/editor/sections/KnowledgeBaseSection";

const KBS: KbSummary[] = [
  { id: "u1", agent_ref: "knowledge_base_aaa", name: "Pricing", status: "complete", source_count: 1, updated_at: "2026-07-03T00:00:00Z" },
  { id: "u2", agent_ref: "knowledge_base_bbb", name: "FAQ", status: "in_progress", source_count: 0, updated_at: "2026-07-03T00:00:00Z" },
];

function Harness({ bound }: { bound: string[] }) {
  const form = useForm<AgentConfigForm>({
    defaultValues: { llm: { model: "m", temperature: null, knowledge_base_ids: bound } } as AgentConfigForm,
  });
  return <KnowledgeBaseSection form={form} />;
}

function renderSection(bound: string[]) {
  getMock.mockImplementation((u: string) =>
    u === "/v1/admin/knowledge-bases" ? Promise.resolve(KBS) : Promise.reject(new Error(u)),
  );
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Harness bound={bound} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("KnowledgeBaseSection", () => {
  it("renders each KB with its bound state", async () => {
    renderSection(["knowledge_base_aaa"]);
    const pricing = (await screen.findByLabelText(/Pricing/)) as HTMLInputElement;
    const faq = screen.getByLabelText(/FAQ/) as HTMLInputElement;
    expect(pricing.checked).toBe(true);
    expect(faq.checked).toBe(false);
  });

  it("binding a KB adds its token to the form value", async () => {
    renderSection([]);
    const faq = (await screen.findByLabelText(/FAQ/)) as HTMLInputElement;
    await userEvent.click(faq);
    expect(faq.checked).toBe(true);
  });

  it("preserves an unknown bound token (not in the list) and shows it", async () => {
    renderSection(["knowledge_base_orphan"]);
    // The orphan token has no matching KB row but must still be surfaced, not dropped.
    expect(await screen.findByText(/knowledge_base_orphan/)).toBeInTheDocument();
  });
});
