// apps/admin-ui/src/test/toolCatalog.test.tsx
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useToolCatalog, type ToolSpec } from "../config/toolCatalog";

const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (u: string) => getMock(u) },
}));

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

const SAMPLE: ToolSpec[] = [
  {
    name: "log_wellness",
    label: "Log wellness",
    description: "Record the elder's wellness.",
    category: "logging",
    always_on: false,
    requires_config: false,
  },
  {
    name: "end_call",
    label: "End call",
    description: "End the call gracefully.",
    category: "lifecycle",
    always_on: true,
    requires_config: false,
  },
];

afterEach(() => {
  vi.restoreAllMocks();
  getMock.mockReset();
});

describe("useToolCatalog", () => {
  it("fetches /v1/admin/tool-catalog and returns the tools", async () => {
    getMock.mockResolvedValue({ tools: SAMPLE });
    const { result } = renderHook(() => useToolCatalog(), { wrapper: wrapper() });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(getMock).toHaveBeenCalledWith("/v1/admin/tool-catalog");
    expect(result.current.data).toEqual(SAMPLE);
  });
});
