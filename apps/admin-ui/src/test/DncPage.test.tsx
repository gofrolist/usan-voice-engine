import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { AdminDNCResponse } from "../types/api";
import { meFixture } from "./meFixture";

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
vi.mock("../components/ui/toast", () => ({ pushToast: vi.fn() }));

import { DncPage } from "../features/dnc/DncPage";

let entries: AdminDNCResponse[] = [];
function renderPage(role: "admin" | "viewer" = "admin") {
  getMock.mockImplementation((u: string) => {
    if (u === "/v1/auth/me") return Promise.resolve(meFixture(role));
    if (u.startsWith("/v1/admin/dnc")) return Promise.resolve(entries);
    return Promise.reject(new Error(u));
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <DncPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  getMock.mockReset();
  postMock.mockReset();
  delMock.mockReset();
  entries = [
    { masked_phone: "***4567", reason: "patient request", added_at: "2026-06-20T09:00:00Z" },
  ];
});
afterEach(() => vi.clearAllMocks());

describe("DncPage", () => {
  it("lists masked entries", async () => {
    renderPage();
    expect(await screen.findByText("***4567")).toBeInTheDocument();
    expect(screen.getByText("patient request")).toBeInTheDocument();
  });

  it("adds a number via the dialog", async () => {
    const user = userEvent.setup();
    postMock.mockResolvedValue({ masked_phone: "***9999", reason: null, added_at: "" });
    renderPage();
    await user.click(await screen.findByRole("button", { name: "+ Add to DNC" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText(/Phone/), "+19495559999");
    await user.click(within(dialog).getByRole("button", { name: "Add" }));
    await waitFor(() =>
      expect(postMock).toHaveBeenCalledWith("/v1/admin/dnc", { phone_e164: "+19495559999", reason: null }),
    );
  });

  it("requires the full E.164 typed to remove", async () => {
    const user = userEvent.setup();
    delMock.mockResolvedValue(undefined);
    renderPage();
    await screen.findByText("***4567");
    await user.click(screen.getByRole("button", { name: "Remove" }));
    const dialog = screen.getByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Remove" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText(/full number/i), "+19495554567");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => expect(delMock).toHaveBeenCalledWith("/v1/admin/dnc/%2B19495554567"));
  });

  it("renders Admins-only for a viewer", async () => {
    renderPage("viewer");
    expect(await screen.findByText("Admins only.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "+ Add to DNC" })).not.toBeInTheDocument();
  });
});
