import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { Me } from "../types/api";

// Route-by-URL api mock — useIsAdmin derives from the real /v1/auth/me query.
const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    get: (u: string) => getMock(u),
  },
}));

import { useIsAdmin } from "../auth/useSession";

let me: Me;

function routeGet(url: string): Promise<unknown> {
  if (url === "/v1/auth/me") return Promise.resolve(me);
  return Promise.reject(new Error(`unexpected GET ${url}`));
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function makeMe(over: Partial<Me> = {}): Me {
  return {
    email: "me@example.com",
    is_super_admin: false,
    acting_as: false,
    active_org: null,
    orgs: [],
    version: "dev",
    ...over,
  };
}

describe("useIsAdmin", () => {
  beforeEach(() => {
    getMock.mockReset();
    getMock.mockImplementation(routeGet);
  });
  afterEach(() => vi.clearAllMocks());

  it("is true for a super-admin even with no active org (acting-as has role null)", async () => {
    me = makeMe({ is_super_admin: true, active_org: null });
    const { result } = renderHook(() => useIsAdmin(), { wrapper });
    await waitFor(() => expect(result.current).toBe(true));
  });

  it("is true for a non-super-admin whose active org role is admin", async () => {
    me = makeMe({
      active_org: { id: "o1", name: "Org One", slug: "org-one", role: "admin" },
    });
    const { result } = renderHook(() => useIsAdmin(), { wrapper });
    await waitFor(() => expect(result.current).toBe(true));
  });

  it("is false for a non-super-admin whose active org role is viewer", async () => {
    me = makeMe({
      active_org: { id: "o1", name: "Org One", slug: "org-one", role: "viewer" },
    });
    const { result } = renderHook(() => useIsAdmin(), { wrapper });
    // Resolve the query, then confirm it stays false.
    await waitFor(() => expect(getMock).toHaveBeenCalledWith("/v1/auth/me"));
    await waitFor(() => expect(result.current).toBe(false));
  });
});
