import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { QueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type {
  CallbackRequestSummary,
  FollowupFlagSummary,
  QueuesSummary,
  QueueStatus,
} from "../../types/api";

export const PAGE_SIZE = 50;

// The only two settable transition targets — "open" is not one (server 422s it).
export type QueueTransition = "acknowledged" | "resolved";

// All three queue queries opt INTO refetchOnWindowFocus: this page is what a
// Grafana alert sends a nurse into, so it must not show stale rows after an
// alt-tab. The global default stays false for every other page
// (lib/queryClient.ts). No refetchInterval — polling is a non-goal (spec §5.4).

export function useFollowUpFlags(
  status: QueueStatus | undefined,
  severity: string | undefined,
  limit: number,
  offset: number,
) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (status) params.set("status", status);
  if (severity) params.set("severity", severity);
  return useQuery<FollowupFlagSummary[]>({
    queryKey: ["admin-flags", status, severity, limit, offset],
    queryFn: () =>
      api.get<FollowupFlagSummary[]>(`/v1/admin/follow-up-flags?${params.toString()}`),
    refetchOnWindowFocus: true,
  });
}

export function useCallbackRequests(status: QueueStatus | undefined, limit: number, offset: number) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (status) params.set("status", status);
  return useQuery<CallbackRequestSummary[]>({
    queryKey: ["admin-callbacks", status, limit, offset],
    queryFn: () =>
      api.get<CallbackRequestSummary[]>(`/v1/admin/callback-requests?${params.toString()}`),
    refetchOnWindowFocus: true,
  });
}

export function useQueuesSummary() {
  return useQuery<QueuesSummary>({
    queryKey: ["admin-queues-summary"],
    queryFn: () => api.get<QueuesSummary>("/v1/admin/queues/summary"),
    refetchOnWindowFocus: true,
  });
}

interface TransitionVars {
  id: number;
  status: QueueTransition;
}

// On 409 the row moved under us (another nurse acted first): say so and refetch
// instead of surfacing the raw conflict detail; other errors surface verbatim.
function onTransitionError(qc: QueryClient, listKey: readonly string[], err: ApiError) {
  if (err.status === 409) {
    pushToast("Status changed elsewhere — list refreshed", "info");
    void qc.invalidateQueries({ queryKey: listKey });
    void qc.invalidateQueries({ queryKey: ["admin-queues-summary"] });
  } else {
    pushToast(err.detail);
  }
}

export function useUpdateFlagStatus() {
  const qc = useQueryClient();
  return useMutation<FollowupFlagSummary, ApiError, TransitionVars>({
    mutationFn: ({ id, status }) =>
      api.patch<FollowupFlagSummary>(`/v1/admin/follow-up-flags/${id}`, { status }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin-flags"] });
      void qc.invalidateQueries({ queryKey: ["admin-queues-summary"] });
    },
    onError: (err) => onTransitionError(qc, ["admin-flags"], err),
  });
}

export function useUpdateCallbackStatus() {
  const qc = useQueryClient();
  return useMutation<CallbackRequestSummary, ApiError, TransitionVars>({
    mutationFn: ({ id, status }) =>
      api.patch<CallbackRequestSummary>(`/v1/admin/callback-requests/${id}`, { status }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["admin-callbacks"] });
      void qc.invalidateQueries({ queryKey: ["admin-queues-summary"] });
    },
    onError: (err) => onTransitionError(qc, ["admin-callbacks"], err),
  });
}
