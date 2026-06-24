import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { AdminDNCResponse, DNCCreate } from "../../types/api";

const DNC_KEY = ["dnc"] as const;

export function useDnc(limit = 200, offset = 0) {
  return useQuery<AdminDNCResponse[]>({
    queryKey: [...DNC_KEY, limit, offset],
    queryFn: () => api.get<AdminDNCResponse[]>(`/v1/admin/dnc?limit=${limit}&offset=${offset}`),
  });
}

export function useAddDnc() {
  const qc = useQueryClient();
  return useMutation<AdminDNCResponse, ApiError, DNCCreate>({
    mutationFn: (body) => api.post<AdminDNCResponse>("/v1/admin/dnc", body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: DNC_KEY }),
  });
}

export function useRemoveDnc() {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    // The path carries the full E.164; the operator re-enters it (the list only has
    // the masked form — spec D5). encodeURIComponent guards the leading '+'.
    mutationFn: (phone) => api.del<void>(`/v1/admin/dnc/${encodeURIComponent(phone)}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: DNC_KEY }),
    onError: (err) => pushToast(err.detail),
  });
}
