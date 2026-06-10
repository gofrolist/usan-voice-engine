import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { AdminCallSummary } from "../../types/api";

export const PAGE_SIZE = 50;

export interface CallsFilters {
  elderId?: string;
  status?: string;
  direction?: string;
  origin?: string;
  createdFrom?: string;
  createdTo?: string; // already exclusive — CallsPage bumps the inclusive To by +1 day
}

// Query keys carry UUIDs/enums/dates ONLY — never names or phones (spec §6.5).
// The global refetchOnWindowFocus: false default (queryClient.ts) stands here.
export function useCalls(filters: CallsFilters, limit: number, offset: number) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (filters.elderId) params.set("elder_id", filters.elderId);
  if (filters.status) params.set("status", filters.status);
  if (filters.direction) params.set("direction", filters.direction);
  if (filters.origin) params.set("origin", filters.origin);
  if (filters.createdFrom) params.set("created_from", filters.createdFrom);
  if (filters.createdTo) params.set("created_to", filters.createdTo);
  return useQuery<AdminCallSummary[]>({
    queryKey: ["admin-calls", filters, limit, offset],
    queryFn: () => api.get<AdminCallSummary[]>(`/v1/admin/calls?${params.toString()}`),
  });
}
