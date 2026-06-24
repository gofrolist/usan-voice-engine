import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type {
  CreateScheduleRequest,
  ScheduleResponse,
  UpdateScheduleRequest,
} from "../../types/api";

export const SCHEDULES_KEY = ["schedules"] as const;

export interface ScheduleFilters {
  contactId?: string;
  slot?: string;
  lastResult?: string;
}

export function useSchedules(filters: ScheduleFilters, limit = 100, offset = 0) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (filters.contactId) params.set("contact_id", filters.contactId);
  if (filters.slot) params.set("slot", filters.slot);
  if (filters.lastResult) params.set("last_result", filters.lastResult);
  return useQuery<ScheduleResponse[]>({
    queryKey: [...SCHEDULES_KEY, filters, limit, offset],
    queryFn: () => api.get<ScheduleResponse[]>(`/v1/admin/schedules?${params.toString()}`),
  });
}

export function useContactSchedules(contactId: string) {
  return useSchedules({ contactId });
}

export function useCreateSchedule() {
  const qc = useQueryClient();
  return useMutation<ScheduleResponse, ApiError, CreateScheduleRequest>({
    mutationFn: (body) => api.post<ScheduleResponse>("/v1/admin/schedules", body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: SCHEDULES_KEY }),
  });
}

export function useUpdateSchedule() {
  const qc = useQueryClient();
  return useMutation<ScheduleResponse, ApiError, { id: string; body: UpdateScheduleRequest }>({
    mutationFn: ({ id, body }) => api.patch<ScheduleResponse>(`/v1/admin/schedules/${id}`, body),
    onSuccess: () => void qc.invalidateQueries({ queryKey: SCHEDULES_KEY }),
  });
}

export function useDeleteSchedule() {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.del<void>(`/v1/admin/schedules/${id}`),
    onSuccess: () => void qc.invalidateQueries({ queryKey: SCHEDULES_KEY }),
    onError: (err) => pushToast(err.detail),
  });
}
