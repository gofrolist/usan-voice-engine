import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SectionRail } from "../features/editor/SectionRail";
import type { SectionKey } from "../config/fieldMeta";

const ORDER: SectionKey[] = ["prompts", "voice", "llm", "voicemail_detection"];

describe("SectionRail", () => {
  it("exposes each section as a tab whose accessible name excludes the summary", () => {
    render(
      <SectionRail
        order={ORDER}
        active="prompts"
        summaries={{ llm: "gemini-3.1-flash-lite" }}
        onSelect={() => {}}
      />,
    );
    // Voicemail tab is named exactly "Voicemail" (the section label) — the same
    // selector ProfileEditorPage.test.tsx and the e2e smoke rely on.
    expect(screen.getByRole("tab", { name: "Voicemail" })).toBeInTheDocument();
    // The LLM summary is rendered but aria-hidden, so the tab name stays "LLM".
    expect(screen.getByRole("tab", { name: "LLM" })).toBeInTheDocument();
    expect(screen.getByText("gemini-3.1-flash-lite")).toHaveAttribute("aria-hidden", "true");
  });

  it("marks the active tab and reports the chosen section key", async () => {
    const onSelect = vi.fn();
    render(<SectionRail order={ORDER} active="prompts" summaries={{}} onSelect={onSelect} />);
    expect(screen.getByRole("tab", { name: "Prompts" })).toHaveAttribute("aria-selected", "true");
    await userEvent.click(screen.getByRole("tab", { name: "Voice" }));
    expect(onSelect).toHaveBeenCalledWith("voice");
  });
});
