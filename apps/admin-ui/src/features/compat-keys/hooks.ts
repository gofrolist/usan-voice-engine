import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { CompatKey, CompatKeyCreated } from "../../types/api";

// Super-admin only (route + server both gate). The list query stays disabled for
// non-super-admins so we never fire a guaranteed 403.
export function useCompatKeys(enabled: boolean) {
  return useQuery<CompatKey[]>({
    queryKey: ["compat-keys"],
    queryFn: () => api.get<CompatKey[]>("/v1/admin/compat-keys"),
    enabled,
  });
}

// Create returns the plaintext token ONCE (CompatKeyCreated.token). The caller holds
// it in component state to show the once-only dialog, then the list invalidates.
export function useCreateCompatKey() {
  const qc = useQueryClient();
  return useMutation<CompatKeyCreated, ApiError, { label: string | null }>({
    mutationFn: (body) => api.post<CompatKeyCreated>("/v1/admin/compat-keys", body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["compat-keys"] });
    },
    onError: (err) => pushToast(err.detail),
  });
}

// Revoke is a 204 (api.del returns undefined). One-way; the list refetches.
export function useRevokeCompatKey() {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.del<void>(`/v1/admin/compat-keys/${id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["compat-keys"] });
    },
    onError: (err) => pushToast(err.detail),
  });
}
