import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { EditorToolbar } from "../features/editor/EditorToolbar";

function setup(overrides: Partial<Parameters<typeof EditorToolbar>[0]> = {}) {
  const props = {
    name: "Sales",
    status: "active",
    publishedVersion: 3 as number | null,
    dirty: false,
    model: "gemini-3.1-flash-lite",
    voice: "default",
    language: "default",
    isAdmin: true,
    saving: false,
    profileId: "p1",
    onJump: vi.fn(),
    onSave: vi.fn(),
    onPublish: vi.fn(),
    ...overrides,
  };
  render(
    <MemoryRouter>
      <EditorToolbar {...props} />
    </MemoryRouter>,
  );
  return props;
}

describe("EditorToolbar", () => {
  it("shows Save draft + Publish for admins and jumps to llm from the Model chip", async () => {
    const props = setup();
    expect(screen.getByRole("button", { name: "Publish" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /save draft/i })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /model/i }));
    expect(props.onJump).toHaveBeenCalledWith("llm");
  });

  it("hides actions and shows a read-only note for viewers", () => {
    setup({ isAdmin: false });
    expect(screen.queryByRole("button", { name: "Publish" })).not.toBeInTheDocument();
    expect(screen.getByText(/read-only/i)).toBeInTheDocument();
  });
});
