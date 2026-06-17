import { NavLink } from "react-router-dom";
import { cn } from "../lib/cn";
import { useSession, useIsAdmin } from "../auth/useSession";
import { api } from "../lib/api";
import { Button } from "./ui/button";
import { ThemeToggle } from "./ui/ThemeToggle";
import { OrgSwitcher } from "./OrgSwitcher";

interface NavItem {
  to: string;
  label: string;
  adminOnly?: boolean;
  superAdminOnly?: boolean;
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
      { to: "/contacts", label: "Contacts", adminOnly: true },
      { to: "/defaults", label: "Defaults" },
      // List view is all-roles (mutations are ADMIN-gated inside the page).
      { to: "/custom-variables", label: "Variables" },
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
      { to: "/members", label: "Members", adminOnly: true },
      { to: "/organizations", label: "Organizations", superAdminOnly: true },
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

function Wordmark() {
  return (
    <div className="flex items-center gap-2.5 px-5 py-4">
      <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-ink text-sm font-bold text-canvas">
        U
      </span>
      <span className="font-display text-[1.05rem] font-semibold tracking-tight text-ink-strong">
        USAN Admin
      </span>
    </div>
  );
}

// Shared sidebar body — used by both the persistent desktop rail and the mobile drawer.
// `onNavigate` lets the drawer close itself when a link is followed.
export function SidebarNav({ onNavigate }: { onNavigate?: () => void }) {
  const { data: me } = useSession();
  const isAdmin = useIsAdmin();
  const isSuperAdmin = !!me?.is_super_admin;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <Wordmark />
      <nav className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-3 py-2">
        {GROUPS.map((group) => {
          const items = group.items.filter(
            (n) => (!n.adminOnly || isAdmin) && (!n.superAdminOnly || isSuperAdmin),
          );
          if (items.length === 0) return null;
          return (
            <div key={group.heading}>
              <div className="px-3 pb-1 text-[11px] font-semibold uppercase tracking-wider text-faint">
                {group.heading}
              </div>
              <div className="flex flex-col gap-0.5">
                {items.map((n) => (
                  <NavLink
                    key={n.to}
                    to={n.to}
                    end={n.to === "/"}
                    onClick={onNavigate}
                    className={({ isActive }) =>
                      cn(
                        "rounded-lg px-3 py-2 text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
                        isActive
                          ? "bg-accent-soft font-medium text-accent"
                          : "text-muted hover:bg-surface-2 hover:text-ink",
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
      <div className="border-t border-line px-4 py-3">
        <OrgSwitcher />
        <div className="mb-2.5 flex items-center justify-between">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-faint">
            Appearance
          </span>
          <ThemeToggle />
        </div>
        <div className="truncate text-sm text-ink" title={me?.email}>
          {me?.email}
        </div>
        {me?.active_org ? (
          <div
            className="truncate text-xs text-faint"
            title={me.active_org.name}
          >
            {me.active_org.name}
            {me.active_org.role ? (
              <span className="uppercase tracking-wide"> · {me.active_org.role}</span>
            ) : null}
          </div>
        ) : null}
        <Button variant="ghost" className="mt-1 px-0" onClick={logout}>
          Log out
        </Button>
      </div>
    </div>
  );
}

// Persistent desktop sidebar. Hidden on mobile — AppLayout renders a slide-over drawer
// (also backed by <SidebarNav>) plus a top bar there instead.
export function NavSidebar() {
  return (
    <aside className="hidden w-52 shrink-0 border-r border-line bg-surface md:flex md:flex-col">
      <SidebarNav />
    </aside>
  );
}
