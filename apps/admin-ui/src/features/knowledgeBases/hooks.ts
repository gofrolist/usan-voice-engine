import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { KbCreate, KbDetail, KbSourceCreate, KbSummary } from "../../types/api";

const KB_KEY = ["knowledge-bases"] as const;

// Poll while any KB is mid-ingestion so its badge flips to complete without a manual
// refresh; stop polling once nothing is in flight.
function listRefetchInterval(rows: KbSummary[] | undefined): number | false {
  return rows?.some((k) => k.status === "in_progress") ? 3000 : false;
}

export function useKnowledgeBases() {
  return useQuery<KbSummary[]>({
    queryKey: KB_KEY,
    queryFn: () => api.get<KbSummary[]>("/v1/admin/knowledge-bases"),
    refetchInterval: (query) => listRefetchInterval(query.state.data),
  });
}

export function useKnowledgeBase(id: string) {
  return useQuery<KbDetail>({
    queryKey: [...KB_KEY, "detail", id],
    queryFn: () => api.get<KbDetail>(`/v1/admin/knowledge-bases/${id}`),
    refetchInterval: (query) => (query.state.data?.status === "in_progress" ? 3000 : false),
  });
}

export function useCreateKb() {
  const qc = useQueryClient();
  return useMutation<KbDetail, ApiError, KbCreate>({
    mutationFn: (body) => api.post<KbDetail>("/v1/admin/knowledge-bases", body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: KB_KEY });
    },
    // 422 (invalid name) surfaces inline in the dialog, so no toast here.
  });
}

export function useDeleteKb() {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.del<void>(`/v1/admin/knowledge-bases/${id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: KB_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}

export function useAddSource(kbId: string) {
  const qc = useQueryClient();
  return useMutation<KbDetail, ApiError, KbSourceCreate>({
    mutationFn: (body) => api.post<KbDetail>(`/v1/admin/knowledge-bases/${kbId}/sources`, body),
    onSuccess: (data) => {
      qc.setQueryData([...KB_KEY, "detail", kbId], data);
      void qc.invalidateQueries({ queryKey: KB_KEY });
    },
  });
}

export function useDeleteSource(kbId: string) {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (sourceId) =>
      api.del<void>(`/v1/admin/knowledge-bases/${kbId}/sources/${sourceId}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [...KB_KEY, "detail", kbId] });
      void qc.invalidateQueries({ queryKey: KB_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}
