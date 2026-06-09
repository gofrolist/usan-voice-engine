import { Outlet } from "react-router-dom";
import { NavSidebar } from "./NavSidebar";
import { ErrorToast } from "./ErrorToast";

// Shell rendered for every authenticated route: sidebar + routed content + the
// global toast outlet. The frame itself does NOT scroll — each routed view owns its
// scroll (simple pages via PageLayout; the editor via its own full-height panes), so
// a pinned editor toolbar can never overlap content. Wrapped by RequireAuth.
export function AppLayout() {
  return (
    <div className="flex h-screen overflow-hidden bg-slate-50 text-slate-900">
      <NavSidebar />
      <main className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <Outlet />
      </main>
      <ErrorToast />
    </div>
  );
}
