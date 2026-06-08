import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { ElderSummary } from "../../types/api";

const ELDERS_KEY = ["elders"] as const;

// Paged: the elders table is the full patient roster, so never fetch it all at once.
// Per-page keys live under the ELDERS_KEY prefix, so the assign mutation's
// invalidateQueries({ queryKey: ELDERS_KEY }) still refreshes every page.
export function useElders(limit: number, offset: number) {
  return useQuery<ElderSummary[]>({
    queryKey: [...ELDERS_KEY, limit, offset],
    queryFn: () =>
      api.get<ElderSummary[]>(`/v1/admin/elders?limit=${limit}&offset=${offset}`),
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
