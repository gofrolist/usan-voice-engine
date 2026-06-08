import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { ElderSummary } from "../../types/api";

const ELDERS_KEY = ["elders"] as const;

export function useElders() {
  return useQuery<ElderSummary[]>({
    queryKey: ELDERS_KEY,
    queryFn: () => api.get<ElderSummary[]>("/v1/admin/elders"),
  });
}

interface AssignVars {
  elderId: string;
  // null clears the assignment (fall back to the per-direction default).
  agentProfileId: string | null;
}

export function useAssignProfile() {
  const qc = useQueryClient();
  return useMutation<ElderSummary, ApiError, AssignVars>({
    mutationFn: ({ elderId, agentProfileId }) =>
      api.put<ElderSummary>(`/v1/admin/elders/${elderId}/profile`, {
        agent_profile_id: agentProfileId,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ELDERS_KEY });
      // Assigned-elder counts on the profiles list change too.
      void qc.invalidateQueries({ queryKey: ["profiles"] });
    },
    onError: (err) => pushToast(err.detail),
  });
}
