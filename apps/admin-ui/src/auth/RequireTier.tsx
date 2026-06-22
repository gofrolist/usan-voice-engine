import type { ReactNode } from "react";
import { Navigate } from "react-router-dom";
import { useSession } from "./useSession";

// Operator-only routes (the Org console). A non-super
// user who deep-links here is sent to /calls — mirrors the backend 403 so a hand-
// typed URL never renders an operator page shell. Renders nothing while the session
// loads (RequireAuth already shows the splash above us) to avoid a premature redirect.
export function RequireSuperAdmin({ children }: { children: ReactNode }) {
  const { data, isLoading } = useSession();
  if (isLoading) return null;
  if (!data?.is_super_admin) return <Navigate to="/calls" replace />;
  return <>{children}</>;
}

// Client-ADMIN routes (Contacts, Members, Audit). A super-admin counts as admin
// everywhere (incl. acting-as, where active_org.role is null). A viewer is sent to
// /calls.
export function RequireAdmin({ children }: { children: ReactNode }) {
  const { data, isLoading } = useSession();
  if (isLoading) return null;
  const isAdmin = !!data && (data.is_super_admin || data.active_org?.role === "admin");
  if (!isAdmin) return <Navigate to="/calls" replace />;
  return <>{children}</>;
}
