import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { AuditEntry } from "../../types/api";

// Filters are applied SERVER-SIDE (not over a fetched window), so a match older than
// the limit window is still found — an audit/compliance screen must not show a false
// "no matching entries".
export function useAudit(limit: number, actor: string, action: string) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (actor.trim()) params.set("actor", actor.trim());
  if (action) params.set("action", action);
  return useQuery<AuditEntry[]>({
    queryKey: ["audit", limit, actor.trim(), action],
    queryFn: () => api.get<AuditEntry[]>(`/v1/admin/audit?${params.toString()}`),
  });
}
