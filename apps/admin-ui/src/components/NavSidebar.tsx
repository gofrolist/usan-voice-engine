import { NavLink } from "react-router-dom";
import { cn } from "../lib/cn";
import { useSession, useIsAdmin } from "../auth/useSession";
import { api } from "../lib/api";
import { Button } from "./ui/button";

interface NavItem {
  to: string;
  label: string;
  adminOnly?: boolean;
}

const NAV: NavItem[] = [
  { to: "/", label: "Profiles" },
  { to: "/elders", label: "Elders", adminOnly: true },
  { to: "/defaults", label: "Defaults" },
  { to: "/audit", label: "Audit" },
  { to: "/admin-users", label: "Admin Users", adminOnly: true },
];

async function logout() {
  try {
    await api.post<void>("/v1/auth/logout");
  } finally {
    window.location.assign("/v1/auth/login");
  }
}

export function NavSidebar() {
  const { data: me } = useSession();
  const isAdmin = useIsAdmin();

  return (
    <aside className="flex w-56 flex-col border-r border-gray-200 bg-white">
      <div className="px-4 py-4 text-lg font-semibold text-gray-900">USAN Admin</div>
      <nav className="flex flex-1 flex-col gap-1 px-2">
        {NAV.filter((n) => !n.adminOnly || isAdmin).map((n) => (
          <NavLink
            key={n.to}
            to={n.to}
            end={n.to === "/"}
            className={({ isActive }) =>
              cn(
                "rounded px-3 py-2 text-sm",
                isActive ? "bg-blue-50 font-medium text-blue-700" : "text-gray-700 hover:bg-gray-100",
              )
            }
          >
            {n.label}
          </NavLink>
        ))}
      </nav>
      <div className="border-t border-gray-200 px-4 py-3 text-sm">
        <div className="truncate text-gray-600" title={me?.email}>
          {me?.email}
        </div>
        <div className="text-xs uppercase text-gray-400">{me?.role}</div>
        <Button variant="ghost" className="mt-2 px-0" onClick={logout}>
          Log out
        </Button>
      </div>
    </aside>
  );
}
