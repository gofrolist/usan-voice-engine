import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";

// Mirrors apps/api/src/usan_api/schemas/variable_catalog.py (VariableSpec). The API is
// authoritative; the frontend fetches the catalog at runtime so the insert-variable
// palette and unknown-variable warnings never hand-duplicate the list.
export interface VariableSpec {
  name: string;
  tier: "builtin" | "custom";
  description: string;
  default: string;
  example: string;
}

interface VariableCatalogResponse {
  variables: VariableSpec[];
}

// Catalog is a global constant on the server (not per-version), so it is highly
// cacheable. Long staleTime avoids refetching it on every editor mount.
const CATALOG_KEY = ["variable-catalog"] as const;

export function useVariableCatalog() {
  return useQuery<VariableSpec[]>({
    queryKey: CATALOG_KEY,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const res = await api.get<VariableCatalogResponse>("/v1/admin/variable-catalog");
      return res.variables;
    },
  });
}

export interface GroupedVariables {
  builtin: VariableSpec[];
  custom: VariableSpec[];
}

// Split into the two palette groups, preserving the server's order within each tier.
export function groupByTier(vars: VariableSpec[] | undefined): GroupedVariables {
  const out: GroupedVariables = { builtin: [], custom: [] };
  for (const v of vars ?? []) {
    out[v.tier].push(v);
  }
  return out;
}
