import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { Invite, InviteCreate } from "../../types/api";

// Pending invites for the caller's active org (server-scoped). Org-switch invalidates
// the whole cache (features/orgs/hooks.ts), so a plain key stays correct.
const INVITES_KEY = ["invites"] as const;

export function useInvites() {
  return useQuery<Invite[]>({
    queryKey: INVITES_KEY,
    queryFn: () => api.get<Invite[]>("/v1/admin/invites"),
  });
}

export function useCreateInvite() {
  const qc = useQueryClient();
  return useMutation<Invite, ApiError, InviteCreate>({
    mutationFn: (body) => api.post<Invite>("/v1/admin/invites", body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: INVITES_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}

export function useRevokeInvite() {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (id) => api.del<void>(`/v1/admin/invites/${encodeURIComponent(id)}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: INVITES_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}

export function useResendInvite() {
  const qc = useQueryClient();
  return useMutation<Invite, ApiError, string>({
    mutationFn: (id) => api.post<Invite>(`/v1/admin/invites/${encodeURIComponent(id)}/resend`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: INVITES_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}
