import { Outlet } from "react-router-dom";
import { NavSidebar } from "./NavSidebar";
import { ErrorToast } from "./ErrorToast";

// Shell rendered for every authenticated route: sidebar + routed content + the
// global toast outlet. Wrapped by RequireAuth in routes.tsx.
export function AppLayout() {
  return (
    <div className="flex h-screen bg-gray-50 text-gray-900">
      <NavSidebar />
      <main className="flex-1 overflow-y-auto p-6">
        <Outlet />
      </main>
      <ErrorToast />
    </div>
  );
}
