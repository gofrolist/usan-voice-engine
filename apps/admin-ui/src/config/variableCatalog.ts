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
  // phi=true marks protected health information. The editor uses this to render a
  // non-blocking warning when a PHI variable appears in a sensitive prompt field
  // (spoken before identity is confirmed, or to voicemail).
  phi: boolean;
}

interface VariableCatalogResponse {
  variables: VariableSpec[];
}

// The catalog is DB-backed on the server (builtins + custom_variables rows) but
// still global (not per-version) and slow-moving, so a long staleTime avoids
// refetching it on every editor mount. Custom-variable CRUD mutations
// (features/customVariables/hooks.ts) invalidate this key so the palette and
// warnings refresh immediately after a change.
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
