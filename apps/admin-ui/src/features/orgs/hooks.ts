import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import type { ApiError } from "../../lib/api";
import { pushToast } from "../../components/ui/toast";
import type { Me, OrgCreate, Organization, SwitchOrgRequest } from "../../types/api";

// Switching the active org changes the result of essentially every query, so on
// success we invalidate the entire cache and let /me refetch the full org list.
// We deliberately do NOT setQueryData(["me"], resp): the switch-org response is a
// partial Me whose `orgs` is [].
export function useSwitchOrg() {
  const qc = useQueryClient();
  return useMutation<Me, ApiError, SwitchOrgRequest>({
    mutationFn: (body) => api.post<Me>("/v1/auth/switch-org", body),
    onSuccess: () => {
      void qc.invalidateQueries();
    },
    onError: (err) => pushToast(err.detail),
  });
}

// Super-admin only (403 otherwise). Callers pass `enabled={me.is_super_admin}` so
// the request is never fired for a regular member.
export function useOrganizations(enabled: boolean) {
  return useQuery<Organization[]>({
    queryKey: ["organizations"],
    queryFn: () => api.get<Organization[]>("/v1/admin/organizations"),
    enabled,
  });
}

// Super-admin only. Creates a new org (optionally seeding its first admin) and
// invalidates the org list so the new row appears. 409 (dup slug) / 422 (bad slug)
// surface via the shared toast.
export function useCreateOrg() {
  const qc = useQueryClient();
  return useMutation<Organization, ApiError, OrgCreate>({
    mutationFn: (body) => api.post<Organization>("/v1/admin/organizations", body),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["organizations"] });
    },
    onError: (err) => pushToast(err.detail),
  });
}
