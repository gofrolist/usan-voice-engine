import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { DefaultsView } from "../../types/api";

// Read-only Defaults-area view (US3 / FR-016..020): per-direction current default,
// resolution order, and the built-in fallback. Server is the source of truth for
// eligibility — the page never recomputes it from the profile list.
export const DEFAULTS_KEY = ["defaults"] as const;

export function useDefaults() {
  return useQuery<DefaultsView>({
    queryKey: DEFAULTS_KEY,
    queryFn: () => api.get<DefaultsView>("/v1/admin/defaults"),
  });
}
