import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";

// Mirrors apps/api/src/usan_api/schemas/model_catalog.py (ModelSpec). The API is
// authoritative; the frontend fetches the catalog at runtime so the LLMSection/STTSection
// selects never hand-duplicate the inventory. Like the voice catalog, an out-of-catalog
// model is NOT a hard client error — the Zod schema keeps llm.model/stt.model plain
// strings (forward-compat: a published config may reference a withdrawn model). The
// server 422 is the authoritative save-time gate (FR-014); the select offers the active
// catalog plus a deprecation marker for a withdrawn currently-selected value.
export interface ModelSpec {
  id: string;
  label: string;
  description: string;
  kind: "llm" | "stt";
  provider: string;
  // Hidden from NEW selection; a published config referencing it still loads.
  deprecated: boolean;
  // Marks the seed default for its kind (informational; the form default is authoritative).
  default: boolean;
}

interface ModelCatalogResponse {
  models: ModelSpec[];
}

// Catalog is a global constant on the server (not per-version), so it is highly
// cacheable. Long staleTime avoids refetching it on every editor mount.
const CATALOG_KEY = ["model-catalog"] as const;

export function useModelCatalog() {
  return useQuery<ModelSpec[]>({
    queryKey: CATALOG_KEY,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const res = await api.get<ModelCatalogResponse>("/v1/admin/model-catalog");
      return res.models;
    },
  });
}

export interface KindGroupedModels {
  llm: ModelSpec[];
  stt: ModelSpec[];
}

// Split into the two select groups, preserving the server's order within each kind.
export function groupByKind(models: ModelSpec[] | undefined): KindGroupedModels {
  const out: KindGroupedModels = { llm: [], stt: [] };
  for (const m of models ?? []) {
    out[m.kind].push(m);
  }
  return out;
}
