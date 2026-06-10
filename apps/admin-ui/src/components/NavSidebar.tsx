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
interface NavGroup {
  heading: string;
  items: NavItem[];
}

const GROUPS: NavGroup[] = [
  { heading: "Build", items: [{ to: "/", label: "Profiles" }] },
  {
    heading: "Config",
    items: [
      { to: "/elders", label: "Elders", adminOnly: true },
      { to: "/defaults", label: "Defaults" },
    ],
  },
  {
    heading: "Operate",
    items: [
      { to: "/calls", label: "Calls" },
      { to: "/queues", label: "Queues" },
    ],
  },
  {
    heading: "System",
    items: [
      { to: "/audit", label: "Audit" },
      { to: "/admin-users", label: "Admin Users", adminOnly: true },
    ],
  },
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
    <aside className="flex w-60 shrink-0 flex-col border-r border-slate-200 bg-white">
      <div className="flex items-center gap-2 px-5 py-4">
        <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-slate-900 text-xs font-bold text-white">
          U
        </span>
        <span className="text-sm font-semibold text-slate-900">USAN Admin</span>
      </div>
      <nav className="flex flex-1 flex-col gap-5 overflow-y-auto px-3 py-2">
        {GROUPS.map((group) => {
          const items = group.items.filter((n) => !n.adminOnly || isAdmin);
          if (items.length === 0) return null;
          return (
            <div key={group.heading}>
              <div className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                {group.heading}
              </div>
              <div className="flex flex-col gap-0.5">
                {items.map((n) => (
                  <NavLink
                    key={n.to}
                    to={n.to}
                    end={n.to === "/"}
                    className={({ isActive }) =>
                      cn(
                        "rounded-lg px-3 py-2 text-sm",
                        isActive
                          ? "bg-indigo-50 font-medium text-indigo-700"
                          : "text-slate-600 hover:bg-slate-100",
                      )
                    }
                  >
                    {n.label}
                  </NavLink>
                ))}
              </div>
            </div>
          );
        })}
      </nav>
      <div className="border-t border-slate-200 px-4 py-3 text-sm">
        <div className="truncate text-slate-700" title={me?.email}>
          {me?.email}
        </div>
        <div className="text-xs uppercase text-slate-400">{me?.role}</div>
        <Button variant="ghost" className="mt-1 px-0" onClick={logout}>
          Log out
        </Button>
      </div>
    </aside>
  );
}
