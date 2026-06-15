// apps/admin-ui/src/test/variableCatalog.test.tsx
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import {
  useVariableCatalog,
  groupByTier,
  type VariableSpec,
} from "../config/variableCatalog";

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

const SAMPLE: VariableSpec[] = [
  {
    name: "first_name",
    tier: "builtin",
    description: "The contact's first name.",
    default: "there",
    example: "Margaret",
    phi: false,
  },
  {
    name: "promo_code",
    tier: "custom",
    description: "Operator-supplied promo code.",
    default: "",
    example: "SPRING",
    phi: false,
  },
];

afterEach(() => {
  vi.restoreAllMocks();
  getMock.mockReset();
});

describe("useVariableCatalog", () => {
  it("fetches /v1/admin/variable-catalog and returns the variables", async () => {
    getMock.mockResolvedValue({ variables: SAMPLE });
    const { result } = renderHook(() => useVariableCatalog(), { wrapper: wrapper() });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(getMock).toHaveBeenCalledWith("/v1/admin/variable-catalog");
    expect(result.current.data).toEqual(SAMPLE);
  });
});

describe("groupByTier", () => {
  it("splits variables into builtin and custom groups, preserving order", () => {
    expect(groupByTier(SAMPLE)).toEqual({
      builtin: [SAMPLE[0]],
      custom: [SAMPLE[1]],
    });
  });

  it("returns empty groups for undefined input", () => {
    expect(groupByTier(undefined)).toEqual({ builtin: [], custom: [] });
  });
});
