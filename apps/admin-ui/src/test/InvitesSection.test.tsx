import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Invite } from "../types/api";

const getMock = vi.fn();
const postMock = vi.fn();
const delMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b?: unknown) => postMock(u, b),
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
const pushToastMock = vi.fn();
vi.mock("../components/ui/toast", () => ({
  pushToast: (m: string, t?: string) => pushToastMock(m, t),
}));

import { InvitesSection } from "../features/invites/InvitesSection";

function invite(over: Partial<Invite> = {}): Invite {
  return {
    id: "inv-1",
    email: "a@example.com",
    role: "viewer",
    status: "pending",
    accept_url: "http://localhost/v1/auth/accept-invite?token=t1",
    expires_at: "2099-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    invited_by: "boss@example.com",
    email_sent: null,
    ...over,
  };
}

let invites: Invite[] = [];

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <InvitesSection />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  postMock.mockReset();
  delMock.mockReset();
  pushToastMock.mockReset();
  getMock.mockImplementation((u: string) => {
    if (u === "/v1/admin/invites") return Promise.resolve(invites);
    return Promise.reject(new Error(`unexpected GET ${u}`));
  });
  invites = [invite()];
});
afterEach(() => vi.clearAllMocks());

describe("InvitesSection", () => {
  it("revoke asks for confirmation before calling DELETE", async () => {
    invites = [invite({ id: "inv-9", email: "revoke-me@example.com" })];
    delMock.mockResolvedValue(undefined);
    renderSection();
    await screen.findByText("revoke-me@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    // A single click must NOT fire the destructive request — a dialog opens first.
    expect(delMock).not.toHaveBeenCalled();
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("revoke-me@example.com")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Revoke" }));
    await waitFor(() => expect(delMock).toHaveBeenCalledWith("/v1/admin/invites/inv-9"));
  });

  it("canceling the revoke dialog does not call DELETE", async () => {
    invites = [invite({ id: "inv-2" })];
    renderSection();
    await screen.findByText("a@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    expect(delMock).not.toHaveBeenCalled();
  });

  function mockClipboard(): ReturnType<typeof vi.fn> {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    return writeText;
  }

  it("invite toasts 'emailed' and skips the clipboard when email_sent is true", async () => {
    const writeText = mockClipboard();
    postMock.mockResolvedValue(invite({ email: "new@x.com", email_sent: true }));
    renderSection();
    await screen.findByText("a@example.com");
    await userEvent.type(screen.getByLabelText("Email"), "new@x.com");
    await userEvent.click(screen.getByRole("button", { name: "Invite" }));
    await waitFor(() =>
      expect(pushToastMock).toHaveBeenCalledWith("Invitation emailed to new@x.com", "info"),
    );
    expect(writeText).not.toHaveBeenCalled(); // emailed: no clipboard fallback
  });

  it("invite falls back to copy + warns when email_sent is false", async () => {
    const writeText = mockClipboard();
    postMock.mockResolvedValue(
      invite({ accept_url: "http://l/accept?token=z", email_sent: false }),
    );
    renderSection();
    await screen.findByText("a@example.com");
    await userEvent.type(screen.getByLabelText("Email"), "f@x.com");
    await userEvent.click(screen.getByRole("button", { name: "Invite" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("http://l/accept?token=z"));
    expect(pushToastMock).toHaveBeenCalledWith(
      "Couldn't email the invite — link copied, send it manually",
      "info",
    );
  });

  it("resend toasts 'emailed' when email_sent is true", async () => {
    invites = [invite({ id: "inv-r", email: "rs@x.com" })];
    postMock.mockResolvedValue(invite({ id: "inv-r", email: "rs@x.com", email_sent: true }));
    renderSection();
    await screen.findByText("rs@x.com");
    await userEvent.click(screen.getByRole("button", { name: "Resend" }));
    await waitFor(() =>
      expect(pushToastMock).toHaveBeenCalledWith("Invitation emailed to rs@x.com", "info"),
    );
  });

  it("resend copies the link (legacy) when email_sent is null", async () => {
    const writeText = mockClipboard();
    invites = [invite({ id: "inv-n" })];
    postMock.mockResolvedValue(
      invite({ id: "inv-n", accept_url: "http://l/accept?token=n", email_sent: null }),
    );
    renderSection();
    await screen.findByText("a@example.com");
    await userEvent.click(screen.getByRole("button", { name: "Resend" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("http://l/accept?token=n"));
    expect(pushToastMock).toHaveBeenCalledWith("Invite link copied", "info");
  });
});
