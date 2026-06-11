// apps/admin-ui/src/test/TranscriptViewer.test.tsx
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { TranscriptViewer } from "../features/calls/TranscriptViewer";
import { fmtDate } from "../lib/format";
import type { TranscriptSegment } from "../types/api";

function seg(over: Partial<TranscriptSegment> = {}): TranscriptSegment {
  return {
    role: "assistant",
    content: "Hello, this is your wellness check-in.",
    tool_name: null,
    tool_args: null,
    started_at: "2026-06-09T10:00:00Z",
    ended_at: "2026-06-09T10:00:05Z",
    ...over,
  };
}

describe("TranscriptViewer", () => {
  // Semantic markers, not Tailwind classes: a pure restyle must not break these
  // tests, and a broken style with intact class names must not pass them.
  it("marks assistant and user segment cards with data-role", () => {
    const { container } = render(
      <TranscriptViewer
        segments={[
          seg({ role: "assistant", content: "How are you feeling today?" }),
          seg({ role: "user", content: "I am doing fine, thanks." }),
        ]}
        callStatus="completed"
      />,
    );

    const assistant = container.querySelectorAll('[data-role="assistant"]');
    const user = container.querySelectorAll('[data-role="user"]');
    expect(assistant).toHaveLength(1);
    expect(user).toHaveLength(1);
    expect(assistant[0]?.textContent).toContain("How are you feeling today?");
    expect(user[0]?.textContent).toContain("I am doing fine, thanks.");
  });

  it("renders tool segments as a monospace chip with collapsed args", () => {
    const { container } = render(
      <TranscriptViewer
        segments={[
          seg({
            role: "assistant",
            tool_name: "flag_for_follow_up",
            tool_args: { severity: "urgent", category: "health" },
          }),
        ]}
        callStatus="completed"
      />,
    );

    const tool = container.querySelector('[data-role="tool"]');
    expect(tool).not.toBeNull();
    expect(screen.getByText("flag_for_follow_up")).toBeInTheDocument();

    const details = container.querySelector("details");
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute("open"); // collapsed by default
    expect(details?.textContent).toContain('"severity"');
    expect(details?.textContent).toContain('"urgent"');
  });

  it("renders the per-segment started_at timestamp via fmtDate", () => {
    render(<TranscriptViewer segments={[seg()]} callStatus="completed" />);

    expect(screen.getByText(fmtDate("2026-06-09T10:00:00Z"))).toBeInTheDocument();
  });

  it("shows the in-progress empty state for a non-terminal call", () => {
    render(<TranscriptViewer segments={[]} callStatus="in_progress" />);

    expect(
      screen.getByText("Call still in progress — transcript appears after the call ends."),
    ).toBeInTheDocument();
  });

  it("shows the no-transcript empty state for a terminal call", () => {
    render(<TranscriptViewer segments={[]} callStatus="completed" />);

    expect(screen.getByText("No transcript was captured for this call.")).toBeInTheDocument();
  });

  it("renders every segment — no virtualization (server caps at 1000)", () => {
    const { container } = render(
      <TranscriptViewer
        segments={[
          seg({ role: "assistant", content: "First" }),
          seg({ role: "user", content: "Second" }),
          seg({ role: "assistant", content: "Third" }),
        ]}
        callStatus="completed"
      />,
    );

    expect(container.querySelectorAll("[data-role]")).toHaveLength(3);
    expect(screen.getByText("First")).toBeInTheDocument();
    expect(screen.getByText("Second")).toBeInTheDocument();
    expect(screen.getByText("Third")).toBeInTheDocument();
  });
});
