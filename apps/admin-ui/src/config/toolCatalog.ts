import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";

// Mirrors apps/api/src/usan_api/schemas/tool_catalog.py (ToolSpec). The API is
// authoritative; the frontend fetches the catalog at runtime so the tool toggles
// never hand-duplicate the inventory. Unlike the variable catalog, this is a CLOSED
// set: enabling a name outside it is a hard validation error server-side.
export interface ToolSpec {
  name: string;
  label: string;
  description: string;
  category: string; // "logging" | "lifecycle" | "safety" | "messaging"
  // end_call is locked on (rendered, cannot be disabled).
  always_on: boolean;
  // send_sms needs >=1 template before the agent offers it.
  requires_config: boolean;
}

interface ToolCatalogResponse {
  tools: ToolSpec[];
}

// Catalog is a global constant on the server (not per-version), so it is highly
// cacheable. Long staleTime avoids refetching it on every editor mount.
const CATALOG_KEY = ["tool-catalog"] as const;

export function useToolCatalog() {
  return useQuery<ToolSpec[]>({
    queryKey: CATALOG_KEY,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const res = await api.get<ToolCatalogResponse>("/v1/admin/tool-catalog");
      return res.tools;
    },
  });
}
