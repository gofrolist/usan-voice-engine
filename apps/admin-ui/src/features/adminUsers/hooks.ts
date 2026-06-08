import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { AdminUser, AdminUserCreate } from "../../types/api";

const KEY = ["admin-users"] as const;

export function useAdminUsers() {
  return useQuery<AdminUser[]>({
    queryKey: KEY,
    queryFn: () => api.get<AdminUser[]>("/v1/admin/admin-users"),
  });
}

export function useAddAdminUser() {
  const qc = useQueryClient();
  return useMutation<AdminUser, ApiError, AdminUserCreate>({
    mutationFn: (body) => api.post<AdminUser>("/v1/admin/admin-users", body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}

export function useRemoveAdminUser() {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (email) => api.del<void>(`/v1/admin/admin-users/${encodeURIComponent(email)}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}
