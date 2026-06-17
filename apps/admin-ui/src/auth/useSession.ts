import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import type { Me } from "../types/api";

// One GET /v1/auth/me. A 401 is handled inside the api wrapper (full-page redirect
// to /v1/auth/login), so it never surfaces as query data here.
export function useSession() {
  return useQuery<Me>({
    queryKey: ["me"],
    queryFn: () => api.get<Me>("/v1/auth/me"),
    staleTime: 60_000,
  });
}

export function useIsAdmin(): boolean {
  const { data } = useSession();
  // A super-admin counts as admin everywhere — including while acting-as another
  // org, where active_org.role is null but is_super_admin is true.
  return !!data && (data.is_super_admin || data.active_org?.role === "admin");
}
