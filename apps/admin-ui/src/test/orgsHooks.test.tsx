import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

// Mock the api layer (no MSW in this repo — see the other test files).
const postMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: {
    post: (u: string, b?: unknown) => postMock(u, b),
    get: vi.fn(),
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
  pushToast: (message: string, tone?: string) => pushToastMock(message, tone),
}));

import { useSwitchOrg } from "../features/orgs/hooks";

beforeEach(() => {
  postMock.mockReset();
  pushToastMock.mockReset();
});
afterEach(() => vi.clearAllMocks());

describe("useSwitchOrg", () => {
  // Switching the active org changes the result of nearly every org-scoped query, so
  // the hook MUST REMOVE (evict) the ENTIRE react-query cache (no queryKey filter), not
  // just invalidate it: query keys are not org-namespaced, so merely invalidating keeps
  // serving the PREVIOUS org's data while the background refetch runs — a cross-org PHI
  // bleed in the UI. removeQueries evicts immediately; active observers then refetch.
  it("removes (evicts) the entire query cache on success", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const removeSpy = vi.spyOn(client, "removeQueries");
    postMock.mockResolvedValue({
      email: "a@x.com",
      is_super_admin: false,
      acting_as: false,
      active_org: { id: "o2", name: "Beta", slug: "beta", role: "admin" },
      orgs: [],
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useSwitchOrg(), { wrapper });
    await result.current.mutateAsync({ organization_id: "o2" });

    await waitFor(() => expect(removeSpy).toHaveBeenCalled());
    expect(postMock).toHaveBeenCalledWith("/v1/auth/switch-org", { organization_id: "o2" });
    // Whole-cache eviction = called with NO filter argument (not a narrow ["me"]).
    expect(removeSpy).toHaveBeenCalledWith();
    expect(removeSpy).not.toHaveBeenCalledWith(expect.objectContaining({ queryKey: ["me"] }));
  });

  it("surfaces a failure as a toast and does not evict the cache", async () => {
    const { ApiError } = await import("../lib/api");
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const removeSpy = vi.spyOn(client, "removeQueries");
    postMock.mockRejectedValue(new ApiError(403, "no access to this organization"));
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );

    const { result } = renderHook(() => useSwitchOrg(), { wrapper });
    await expect(result.current.mutateAsync({ organization_id: "o2" })).rejects.toThrow();

    await waitFor(() =>
      expect(pushToastMock).toHaveBeenCalledWith("no access to this organization", undefined),
    );
    expect(removeSpy).not.toHaveBeenCalled();
  });
});
