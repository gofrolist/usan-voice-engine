import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { AuditEntry } from "../../types/api";

export function useAudit(limit: number) {
  return useQuery<AuditEntry[]>({
    queryKey: ["audit", limit],
    queryFn: () => api.get<AuditEntry[]>(`/v1/admin/audit?limit=${limit}`),
  });
}
