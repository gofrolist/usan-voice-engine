// apps/admin-ui/src/test/PromptsSection.test.tsx
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import type { AgentConfigForm } from "../config/agentConfigSchema";

const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (u: string) => getMock(u) },
}));

import { PromptsSection } from "../features/editor/sections/PromptsSection";

const CATALOG = {
  variables: [
    {
      name: "first_name",
      tier: "builtin",
      description: "The elder's first name.",
      default: "there",
      example: "Margaret",
      phi: false,
    },
    {
      name: "today_meds",
      tier: "builtin",
      description: "Medications scheduled today.",
      default: "",
      example: "Lisinopril",
      phi: true,
    },
  ],
};

function wrap(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function Harness({
  greeting,
  voicemailMessage = "v",
  systemPrompt = "sys",
}: {
  greeting: string;
  voicemailMessage?: string;
  systemPrompt?: string;
}) {
  const form = useForm<AgentConfigForm>({
    defaultValues: {
      prompts: {
        system_prompt: systemPrompt,
        greeting,
        recording_disclosure: "r",
        voicemail_message: voicemailMessage,
        checkin_flow_instructions: "f",
        goodbye_message: "g",
        inbound_opening: "o",
        inbound_personalization_template: "with {elder_name}",
      },
    },
  });
  return <PromptsSection form={form} />;
}

describe("PromptsSection catalog wiring", () => {
  it("renders insert-variable buttons once the catalog loads", async () => {
    getMock.mockResolvedValue(CATALOG);
    render(wrap(<Harness greeting="Hello" />));

    await waitFor(() =>
      expect(getMock).toHaveBeenCalledWith("/v1/admin/variable-catalog"),
    );
    // One palette per prompt field (8 fields).
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: /insert variable/i }).length).toBe(8),
    );
  });

  it("shows an unknown-variable notice for a token not in the catalog", async () => {
    getMock.mockResolvedValue(CATALOG);
    render(wrap(<Harness greeting="Hi {{made_up}}" />));

    expect(await screen.findByText(/unknown variable: made_up/i)).toBeInTheDocument();
  });

  it("shows PHI warning when a PHI var appears in the voicemail_message field", async () => {
    getMock.mockResolvedValue(CATALOG);
    render(wrap(<Harness greeting="Hello" voicemailMessage="Meds: {{today_meds}}" />));

    expect(await screen.findByText(/today_meds.*health information/i)).toBeInTheDocument();
  });

  it("does NOT show PHI warning for a PHI var in the system_prompt field", async () => {
    getMock.mockResolvedValue(CATALOG);
    render(wrap(<Harness greeting="Hello" systemPrompt="Context: {{today_meds}}" />));

    // Wait for catalog to load, then assert no warning.
    await waitFor(() =>
      expect(getMock).toHaveBeenCalledWith("/v1/admin/variable-catalog"),
    );
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: /insert variable/i }).length).toBe(8),
    );
    expect(screen.queryByText(/health information/i)).not.toBeInTheDocument();
  });
});
