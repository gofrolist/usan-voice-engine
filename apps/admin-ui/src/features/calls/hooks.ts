import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { AdminCallDetail, AdminCallSummary } from "../../types/api";

export const PAGE_SIZE = 50;

// CallStatus values where the call has not finished. Transcript segments are
// bulk-inserted and recordings finalized post-call, so empty transcript/recording
// on one of these statuses means "not yet", not "none" (spec §5.3).
const NON_TERMINAL_CALL_STATUSES = new Set(["queued", "dialing", "ringing", "in_progress"]);

export function isCallInProgress(status: string): boolean {
  return NON_TERMINAL_CALL_STATUSES.has(status);
}

export interface CallsFilters {
  contactId?: string;
  status?: string;
  direction?: string;
  origin?: string;
  createdFrom?: string; // ISO instant — local midnight of the picked day (CallsPage)
  createdTo?: string; // ISO instant, already exclusive — CallsPage bumps the inclusive To +1 day
}

// Query keys carry UUIDs/enums/dates ONLY — never names or phones (spec §6.5).
// The global refetchOnWindowFocus: false default (queryClient.ts) stands here.
export function useCalls(filters: CallsFilters, limit: number, offset: number) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (filters.contactId) params.set("contact_id", filters.contactId);
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

// Each detail fetch re-signs a bearer recording URL and writes audit rows server-side,
// so this query must NOT opt into focus-refetch — the global default
// refetchOnWindowFocus: false (queryClient.ts) stands here (spec §5.3).
export function useCall(id: string) {
  return useQuery<AdminCallDetail>({
    queryKey: ["admin-call", id],
    queryFn: () => api.get<AdminCallDetail>(`/v1/admin/calls/${id}`),
  });
}
