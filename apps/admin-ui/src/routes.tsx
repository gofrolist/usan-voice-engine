import { createBrowserRouter } from "react-router-dom";
import { RequireAuth } from "./auth/RequireAuth";
import { AppLayout } from "./components/AppLayout";
import { ProfilesListPage } from "./features/profiles/ProfilesListPage";
import { ProfileEditorPage } from "./features/editor/ProfileEditorPage";
import { VersionHistoryPage } from "./features/versions/VersionHistoryPage";
import { EldersPage } from "./features/elders/EldersPage";
import { DefaultsPage } from "./features/defaults/DefaultsPage";
import { AuditPage } from "./features/audit/AuditPage";
import { AdminUsersPage } from "./features/adminUsers/AdminUsersPage";

// All routes are gated by RequireAuth and rendered inside AppLayout. The api
// wrapper handles 401 with a full-page redirect to /v1/auth/login.
export const router = createBrowserRouter([
  {
    path: "/",
    element: (
      <RequireAuth>
        <AppLayout />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <ProfilesListPage /> },
      { path: "profiles/:id", element: <ProfileEditorPage /> },
      { path: "profiles/:id/versions", element: <VersionHistoryPage /> },
      { path: "elders", element: <EldersPage /> },
      { path: "defaults", element: <DefaultsPage /> },
      { path: "audit", element: <AuditPage /> },
      { path: "admin-users", element: <AdminUsersPage /> },
    ],
  },
]);
