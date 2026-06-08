import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { ProfileCreate, ProfileDetail, ProfileSummary, SetDefaultRequest } from "../../types/api";

const LIST_KEY = ["profiles"] as const;

function onApiError(err: unknown): void {
  pushToast((err as ApiError)?.detail ?? "Request failed");
}

export function useProfiles() {
  return useQuery<ProfileSummary[]>({
    queryKey: LIST_KEY,
    queryFn: () => api.get<ProfileSummary[]>("/v1/admin/profiles"),
  });
}

export function useCreateProfile() {
  const qc = useQueryClient();
  return useMutation<ProfileSummary, ApiError, ProfileCreate>({
    mutationFn: (body) => api.post<ProfileSummary>("/v1/admin/profiles", body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: LIST_KEY });
    },
    onError: onApiError,
  });
}

export function useArchiveProfile() {
  const qc = useQueryClient();
  return useMutation<ProfileDetail, ApiError, string>({
    mutationFn: (id) => api.post<ProfileDetail>(`/v1/admin/profiles/${id}/archive`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: LIST_KEY });
    },
    onError: onApiError,
  });
}

interface SetDefaultVars {
  id: string;
  direction: SetDefaultRequest["direction"];
}

export function useSetDefault() {
  const qc = useQueryClient();
  return useMutation<ProfileDetail, ApiError, SetDefaultVars>({
    mutationFn: ({ id, direction }) =>
      api.post<ProfileDetail>(`/v1/admin/profiles/${id}/set-default`, { direction }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: LIST_KEY });
    },
    onError: onApiError,
  });
}
