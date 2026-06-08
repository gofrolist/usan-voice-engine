import type { ReactNode } from "react";
import { useSession } from "./useSession";
import { Spinner } from "../components/ui/spinner";

// Gates the app behind a valid session. While GET /v1/auth/me is in flight we show
// a splash; a 401 is already handled by the api wrapper (redirect to login), so a
// hard error here just means the session probe failed for another reason.
export function RequireAuth({ children }: { children: ReactNode }) {
  const { isLoading, isError, error } = useSession();

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <Spinner />
        <span className="ml-3 text-gray-600">Loading…</span>
      </div>
    );
  }

  if (isError) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="text-center text-gray-700">
          <p className="font-medium">Could not load your session.</p>
          <p className="mt-1 text-sm text-gray-500">{(error as Error)?.message}</p>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
