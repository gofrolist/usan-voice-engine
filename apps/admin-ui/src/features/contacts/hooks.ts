import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { ContactSummary } from "../../types/api";

const CONTACTS_KEY = ["contacts"] as const;

// Paged: the contacts table is the full patient roster, so never fetch it all at once.
// Per-page keys live under the CONTACTS_KEY prefix, so the assign mutation's
// invalidateQueries({ queryKey: CONTACTS_KEY }) still refreshes every page.
export function useContacts(limit: number, offset: number) {
  return useQuery<ContactSummary[]>({
    queryKey: [...CONTACTS_KEY, limit, offset],
    queryFn: () =>
      api.get<ContactSummary[]>(`/v1/admin/contacts?limit=${limit}&offset=${offset}`),
  });
}

interface AssignVars {
  contactId: string;
  // null clears the assignment (fall back to the per-direction default).
  agentProfileId: string | null;
}

export function useAssignProfile() {
  const qc = useQueryClient();
  return useMutation<ContactSummary, ApiError, AssignVars>({
    mutationFn: ({ contactId, agentProfileId }) =>
      api.put<ContactSummary>(`/v1/admin/contacts/${contactId}/profile`, {
        agent_profile_id: agentProfileId,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: CONTACTS_KEY });
      // Assigned-contact counts on the profiles list change too.
      void qc.invalidateQueries({ queryKey: ["profiles"] });
    },
    onError: (err) => pushToast(err.detail),
  });
}
