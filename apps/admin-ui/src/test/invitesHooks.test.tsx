import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const postMock = vi.fn();
const getMock = vi.fn();
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

import { useCreateInvite, useInvites, useRevokeInvite } from "../features/invites/hooks";

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return {
    client,
    Wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  };
}

beforeEach(() => {
  postMock.mockReset();
  getMock.mockReset();
  delMock.mockReset();
  pushToastMock.mockReset();
});
afterEach(() => vi.clearAllMocks());

describe("invite hooks", () => {
  it("useInvites GETs /v1/admin/invites", async () => {
    getMock.mockResolvedValue([]);
    const { Wrapper } = wrapper();
    const { result } = renderHook(() => useInvites(), { wrapper: Wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(getMock).toHaveBeenCalledWith("/v1/admin/invites");
  });

  it("useCreateInvite POSTs and invalidates the invites key", async () => {
    postMock.mockResolvedValue({ id: "1", email: "a@x.com" });
    const { client, Wrapper } = wrapper();
    const spy = vi.spyOn(client, "invalidateQueries");
    const { result } = renderHook(() => useCreateInvite(), { wrapper: Wrapper });
    result.current.mutate({ email: "a@x.com", role: "viewer" });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(postMock).toHaveBeenCalledWith("/v1/admin/invites", { email: "a@x.com", role: "viewer" });
    expect(spy).toHaveBeenCalledWith({ queryKey: ["invites"] });
  });

  it("useRevokeInvite DELETEs by id", async () => {
    delMock.mockResolvedValue(undefined);
    const { Wrapper } = wrapper();
    const { result } = renderHook(() => useRevokeInvite(), { wrapper: Wrapper });
    result.current.mutate("the-id");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(delMock).toHaveBeenCalledWith("/v1/admin/invites/the-id");
  });
});
