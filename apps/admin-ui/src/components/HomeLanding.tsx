import { Navigate } from "react-router-dom";
import { useSession } from "../auth/useSession";
import { ProfilesListPage } from "../features/profiles/ProfilesListPage";

// The index route ("/"). Operators land on Profiles (unchanged); client-org users
// cannot see Profiles (operator-only in P4), so they are sent to their call history.
export function HomeLanding() {
  const { data, isLoading } = useSession();
  if (isLoading) return null;
  if (!data?.is_super_admin) return <Navigate to="/calls" replace />;
  return <ProfilesListPage />;
}
