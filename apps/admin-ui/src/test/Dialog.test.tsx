// apps/admin-ui/src/test/Dialog.test.tsx
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Dialog } from "../components/ui/dialog";

describe("Dialog", () => {
  it("renders the title + children when open", () => {
    render(
      <Dialog open onClose={() => {}} title="Archive profile?">
        <p>body content</p>
      </Dialog>,
    );
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("Archive profile?")).toBeInTheDocument();
    expect(screen.getByText("body content")).toBeInTheDocument();
  });

  it("renders nothing when closed", () => {
    render(
      <Dialog open={false} onClose={() => {}}>
        body
      </Dialog>,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes on Escape even when focus is outside the panel (document-level listener)", async () => {
    const onClose = vi.fn();
    const user = userEvent.setup();
    render(
      <Dialog open onClose={onClose} title="Hi">
        body
      </Dialog>,
    );
    // Focus is on document.body, not inside the dialog panel — the old backdrop-only
    // onKeyDown would never have fired here.
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
