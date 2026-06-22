import { useSession } from "../auth/useSession";
import { ProfilesListPage } from "../features/profiles/ProfilesListPage";

// The index route ("/"). Every member lands on the Profiles list — an org ADMIN authors
// their own org's profiles, a VIEWER sees them read-only, a super-admin sees the inventory.
// Backend RLS scopes the list to the caller's active org; in-screen useIsAdmin() gates writes.
export function HomeLanding() {
  const { isLoading } = useSession();
  if (isLoading) return null;
  return <ProfilesListPage />;
}
