import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type {
  ContactCreate,
  ContactDetail,
  ContactSummary,
  ContactUpdate,
} from "../../types/api";

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

interface SetTimezoneVars {
  contactId: string;
  timezone: string;
}

export function useSetTimezone() {
  const qc = useQueryClient();
  return useMutation<ContactSummary, ApiError, SetTimezoneVars>({
    mutationFn: ({ contactId, timezone }) =>
      api.put<ContactSummary>(`/v1/admin/contacts/${contactId}/timezone`, { timezone }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: CONTACTS_KEY });
    },
    // A 422 (invalid IANA zone) from the API surfaces as a toast.
    onError: (err) => pushToast(err.detail),
  });
}

export function useContact(id: string) {
  return useQuery<ContactDetail>({
    queryKey: [...CONTACTS_KEY, "detail", id],
    queryFn: () => api.get<ContactDetail>(`/v1/admin/contacts/${id}`),
  });
}

export function useCreateContact() {
  const qc = useQueryClient();
  return useMutation<ContactDetail, ApiError, ContactCreate>({
    mutationFn: (body) => api.post<ContactDetail>("/v1/admin/contacts", body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: CONTACTS_KEY });
    },
    // 409 (dup phone/external_id) / 422 surface inline in the dialog, so no toast here.
  });
}

export function useUpdateContact() {
  const qc = useQueryClient();
  return useMutation<ContactDetail, ApiError, { id: string; body: ContactUpdate }>({
    mutationFn: ({ id, body }) => api.patch<ContactDetail>(`/v1/admin/contacts/${id}`, body),
    onSuccess: (_data, { id }) => {
      void qc.invalidateQueries({ queryKey: CONTACTS_KEY });
      void qc.invalidateQueries({ queryKey: [...CONTACTS_KEY, "detail", id] });
    },
  });
}
