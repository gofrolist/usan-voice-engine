import { createBrowserRouter } from "react-router-dom";
import { RequireAuth } from "./auth/RequireAuth";
import { AppLayout } from "./components/AppLayout";
import { PageLayout } from "./components/PageLayout";
import { ProfilesListPage } from "./features/profiles/ProfilesListPage";
import { ProfileEditorPage } from "./features/editor/ProfileEditorPage";
import { VersionHistoryPage } from "./features/versions/VersionHistoryPage";
import { EldersPage } from "./features/elders/EldersPage";
import { DefaultsPage } from "./features/defaults/DefaultsPage";
import { AuditPage } from "./features/audit/AuditPage";
import { AdminUsersPage } from "./features/adminUsers/AdminUsersPage";

// All routes are gated by RequireAuth and rendered inside AppLayout. The api
// wrapper handles 401 with a full-page redirect to /v1/auth/login.
//
// Two shapes share the AppLayout frame: simple pages nest under PageLayout (a
// scrolling, max-width body), while the profile editor renders directly in the
// non-scrolling frame so its toolbar + 2-pane body own their height/scroll.
export const router = createBrowserRouter([
  {
    path: "/",
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [
      {
        element: <PageLayout />,
        children: [
          { index: true, element: <ProfilesListPage /> },
          { path: "profiles/:id/versions", element: <VersionHistoryPage /> },
          { path: "elders", element: <EldersPage /> },
          { path: "defaults", element: <DefaultsPage /> },
          { path: "audit", element: <AuditPage /> },
          { path: "admin-users", element: <AdminUsersPage /> },
        ],
      },
      { path: "profiles/:id", element: <ProfileEditorPage /> },
    ],
  },
]);
