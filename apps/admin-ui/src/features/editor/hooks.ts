import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type {
  AgentConfig,
  DraftUpdate,
  ProfileDetail,
  PublishRequest,
  VersionDetail,
  VersionSummary,
} from "../../types/api";

export const profileKey = (id: string) => ["profile", id] as const;
export const versionKey = (id: string, v: number) => ["profile", id, "version", v] as const;

function onApiError(err: unknown): void {
  pushToast((err as ApiError)?.detail ?? "Request failed");
}

export function useProfile(id: string) {
  return useQuery<ProfileDetail>({
    queryKey: profileKey(id),
    queryFn: () => api.get<ProfileDetail>(`/v1/admin/profiles/${id}`),
  });
}

interface SaveDraftVars {
  config: AgentConfig;
  description?: string | null;
}

export function useSaveDraft(id: string) {
  const qc = useQueryClient();
  return useMutation<ProfileDetail, ApiError, SaveDraftVars>({
    mutationFn: (vars) => {
      const body: DraftUpdate = { config: vars.config, description: vars.description };
      return api.put<ProfileDetail>(`/v1/admin/profiles/${id}/draft`, body);
    },
    onSuccess: (detail) => {
      qc.setQueryData(profileKey(id), detail);
      void qc.invalidateQueries({ queryKey: ["profiles"] });
    },
    // Errors (incl. 422) bubble to the caller so the editor can map field errors.
  });
}

export function usePublish(id: string) {
  const qc = useQueryClient();
  return useMutation<VersionSummary, ApiError, PublishRequest>({
    mutationFn: (body) => api.post<VersionSummary>(`/v1/admin/profiles/${id}/publish`, body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: profileKey(id) });
      void qc.invalidateQueries({ queryKey: ["profiles"] });
      void qc.invalidateQueries({ queryKey: ["versions", id] });
    },
    onError: onApiError,
  });
}

// Fetch a single published version's full config (used by the publish diff).
export function useVersion(id: string, version: number | null) {
  return useQuery<VersionDetail>({
    queryKey: versionKey(id, version ?? -1),
    queryFn: () => api.get<VersionDetail>(`/v1/admin/profiles/${id}/versions/${version}`),
    enabled: version !== null && version > 0,
  });
}
