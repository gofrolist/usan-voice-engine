import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import { profileKey } from "../editor/hooks";
import type { VersionDetail, VersionSummary } from "../../types/api";

export const versionsKey = (id: string) => ["versions", id] as const;
export const versionDetailKey = (id: string, v: number) => ["versions", id, v] as const;

export function useVersions(id: string) {
  return useQuery<VersionSummary[]>({
    queryKey: versionsKey(id),
    queryFn: () => api.get<VersionSummary[]>(`/v1/admin/profiles/${id}/versions`),
  });
}

export function useVersion(id: string, version: number | null) {
  return useQuery<VersionDetail>({
    queryKey: versionDetailKey(id, version ?? -1),
    queryFn: () => api.get<VersionDetail>(`/v1/admin/profiles/${id}/versions/${version}`),
    enabled: version !== null && version > 0,
  });
}

export function useRollback(id: string) {
  const qc = useQueryClient();
  return useMutation<VersionSummary, ApiError, number>({
    mutationFn: (version) =>
      api.post<VersionSummary>(`/v1/admin/profiles/${id}/rollback/${version}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: profileKey(id) });
      void qc.invalidateQueries({ queryKey: versionsKey(id) });
      void qc.invalidateQueries({ queryKey: ["profiles"] });
    },
    onError: (err) => pushToast(err.detail),
  });
}
