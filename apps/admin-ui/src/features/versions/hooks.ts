import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import { profileKey } from "../editor/hooks";
import type { VersionSummary } from "../../types/api";

// The single-version hook lives in editor/hooks (the publish diff uses it); re-export
// it here so the history page and the publish diff share one query key + one fetch.
export { useVersion } from "../editor/hooks";

export const versionsKey = (id: string) => ["versions", id] as const;

export function useVersions(id: string) {
  return useQuery<VersionSummary[]>({
    queryKey: versionsKey(id),
    queryFn: () => api.get<VersionSummary[]>(`/v1/admin/profiles/${id}/versions`),
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
