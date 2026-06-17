import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { Member, MemberCreate, MemberRoleUpdate } from "../../types/api";

// Members of the caller's *active* org (the server scopes by the request's org).
// Switching orgs invalidates everything (see features/orgs/hooks.ts), so a plain
// key without the org id stays correct.
const MEMBERS_KEY = ["members"] as const;

export function useMembers() {
  return useQuery<Member[]>({
    queryKey: MEMBERS_KEY,
    queryFn: () => api.get<Member[]>("/v1/admin/members"),
  });
}

export function useAddMember() {
  const qc = useQueryClient();
  return useMutation<Member, ApiError, MemberCreate>({
    mutationFn: (body) => api.post<Member>("/v1/admin/members", body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: MEMBERS_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}

interface SetRoleVars {
  email: string;
  role: MemberRoleUpdate["role"];
}

export function useSetMemberRole() {
  const qc = useQueryClient();
  return useMutation<Member, ApiError, SetRoleVars>({
    mutationFn: ({ email, role }) =>
      api.patch<Member>(`/v1/admin/members/${encodeURIComponent(email)}`, { role }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: MEMBERS_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}

export function useRemoveMember() {
  const qc = useQueryClient();
  return useMutation<void, ApiError, string>({
    mutationFn: (email) => api.del<void>(`/v1/admin/members/${encodeURIComponent(email)}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: MEMBERS_KEY });
    },
    onError: (err) => pushToast(err.detail),
  });
}
