import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { ContactDetail } from "../types/api";

const postMock = vi.fn();
const patchMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    post: (u: string, b?: unknown) => postMock(u, b),
    patch: (u: string, b?: unknown) => patchMock(u, b),
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

import { ContactFormDialog } from "../features/contacts/ContactFormDialog";

const existing: ContactDetail = {
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

function renderDialog(node: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{node}</QueryClientProvider>);
}

beforeEach(() => {
  postMock.mockReset();
  patchMock.mockReset();
});
afterEach(() => vi.clearAllMocks());

describe("ContactFormDialog — create", () => {
  it("requires a valid E.164 phone and POSTs the contact", async () => {
    const user = userEvent.setup();
    postMock.mockResolvedValue({ ...existing, name: "New Person" });
    const onClose = vi.fn();
    renderDialog(<ContactFormDialog mode="create" onClose={onClose} />);

    await user.type(screen.getByLabelText("Name"), "New Person");
    await user.type(screen.getByLabelText(/Phone/), "555");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(postMock).not.toHaveBeenCalled();
    expect(screen.getByText(/E\.164/)).toBeInTheDocument();

    await user.clear(screen.getByLabelText(/Phone/));
    await user.type(screen.getByLabelText(/Phone/), "+19495551234");
    await user.type(screen.getByLabelText("Timezone"), "America/New_York");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/contacts", {
        name: "New Person",
        phone_e164: "+19495551234",
        timezone: "America/New_York",
      }),
    );
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("surfaces a 409 duplicate as an inline error", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("../lib/api");
    postMock.mockRejectedValue(new ApiError(409, "phone already in use"));
    renderDialog(<ContactFormDialog mode="create" onClose={vi.fn()} />);
    await user.type(screen.getByLabelText("Name"), "Dup");
    await user.type(screen.getByLabelText(/Phone/), "+19495551234");
    await user.type(screen.getByLabelText("Timezone"), "America/New_York");
    await user.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(screen.getByText("phone already in use")).toBeInTheDocument());
  });
});

describe("ContactFormDialog — edit", () => {
  it("starts the phone field empty and OMITS phone_e164 when left blank", async () => {
    const user = userEvent.setup();
    patchMock.mockResolvedValue(existing);
    renderDialog(<ContactFormDialog mode="edit" contact={existing} onClose={vi.fn()} />);

    const phone = screen.getByLabelText(/Phone/) as HTMLInputElement;
    expect(phone.value).toBe("");
    expect(screen.getByText(/\*\*\*4567/)).toBeInTheDocument();

    await user.clear(screen.getByLabelText("Name"));
    await user.type(screen.getByLabelText("Name"), "Edna M.");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1));
    const [url, body] = patchMock.mock.calls[0]!;
    expect(url).toBe(`/v1/admin/contacts/${existing.id}`);
    expect(body).toMatchObject({ name: "Edna M." });
    expect("phone_e164" in body).toBe(false);
  });

  it("rejects an unparseable metadata JSON before submit", async () => {
    const user = userEvent.setup();
    renderDialog(<ContactFormDialog mode="edit" contact={existing} onClose={vi.fn()} />);
    await user.type(screen.getByLabelText(/Metadata/), "{{not json");
    await user.click(screen.getByRole("button", { name: "Save" }));
    expect(patchMock).not.toHaveBeenCalled();
    expect(screen.getByText(/valid JSON object/)).toBeInTheDocument();
  });
});
