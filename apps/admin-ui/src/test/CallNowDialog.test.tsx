import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ContactDetail } from "../types/api";

const postMock = vi.fn();
const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
    post: (u: string, b?: unknown) => postMock(u, b),
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

import { CallNowDialog } from "../features/contacts/CallNowDialog";

const contact: ContactDetail = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "Edna Moore",
  masked_phone: "***4567",
  timezone: "America/New_York",
  agent_profile_id: null,
  agent_profile_name: null,
  external_id: null,
  preferred_voice: null,
  metadata: {},
  created_at: "2026-06-20T09:00:00Z",
  updated_at: "2026-06-20T09:00:00Z",
};

function renderDialog() {
  const onClose = vi.fn();
  getMock.mockImplementation((u: string) =>
    u.startsWith("/v1/admin/profiles") ? Promise.resolve([]) : Promise.reject(new Error(u)),
  );
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <CallNowDialog contact={contact} onClose={onClose} />
    </QueryClientProvider>,
  );
  return { onClose };
}

beforeEach(() => {
  postMock.mockReset();
  getMock.mockReset();
  pushToastMock.mockReset();
});
afterEach(() => vi.clearAllMocks());

describe("CallNowDialog", () => {
  it("disables Call until the out-of-window ack is checked", async () => {
    const user = userEvent.setup();
    postMock.mockResolvedValue({
      id: "c1",
      contact_id: contact.id,
      direction: "outbound",
      status: "queued",
      created_at: "",
    });
    const { onClose } = renderDialog();
    const callBtn = await screen.findByRole("button", { name: /^Call/ });
    expect(callBtn).toBeDisabled();
    await user.click(screen.getByLabelText(/outside their normal window/i));
    expect(callBtn).toBeEnabled();
    await user.click(callBtn);
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/calls", { contact_id: contact.id }),
    );
    expect(pushToastMock).toHaveBeenCalledWith("Call queued.", "info");
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("surfaces a DNC-blocked result inline (not as an error)", async () => {
    const user = userEvent.setup();
    postMock.mockResolvedValue({
      id: "c2",
      contact_id: contact.id,
      direction: "outbound",
      status: "dnc_blocked",
      created_at: "",
    });
    renderDialog();
    await user.click(await screen.findByLabelText(/outside their normal window/i));
    await user.click(screen.getByRole("button", { name: /^Call/ }));
    expect(await screen.findByText(/Do-Not-Call list/)).toBeInTheDocument();
    expect(pushToastMock).not.toHaveBeenCalled();
  });

  it("surfaces a 503 (telephony unavailable) inline", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("../lib/api");
    postMock.mockRejectedValue(new ApiError(503, "outbound calling is not available"));
    renderDialog();
    await user.click(await screen.findByLabelText(/outside their normal window/i));
    await user.click(screen.getByRole("button", { name: /^Call/ }));
    expect(await screen.findByText("outbound calling is not available")).toBeInTheDocument();
  });
});
