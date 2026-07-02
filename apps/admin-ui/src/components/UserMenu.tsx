import { useEffect, useRef, useState } from "react";
import { cn } from "../lib/cn";
import { toggleTheme, useTheme } from "../lib/theme";
import type { Me } from "../types/api";

// Compact sidebar-footer identity: an avatar + email + role, with a "⋯" button that
// opens a small popover holding the theme toggle and log out. Replaces the old stacked
// APPEARANCE label + full-width Log out button to keep the footer dense and readable.
export function UserMenu({ me, onLogout }: { me: Me | undefined; onLogout: () => void }) {
  const [open, setOpen] = useState(false);
  const theme = useTheme();
  const isDark = theme === "dark";
  const rootRef = useRef<HTMLDivElement>(null);

  // Dismiss on outside-click or Escape while the menu is open.
  useEffect(() => {
    if (!open) return;
    function onPointer(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const email = me?.email ?? "";
  const initial = email.charAt(0).toUpperCase() || "?";
  const role = me?.active_org?.role;

  return (
    <div ref={rootRef} className="relative">
      <div className="flex items-center gap-2.5">
        <span
          aria-hidden="true"
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-ink text-xs font-bold text-canvas"
        >
          {initial}
        </span>
        <div className="min-w-0 flex-1 leading-tight">
          <div className="truncate text-sm text-ink" title={email}>
            {email}
          </div>
          {role ? (
            <div className="text-[11px] font-semibold uppercase tracking-wide text-faint">
              {role}
            </div>
          ) : null}
        </div>
        <button
          type="button"
          aria-label="Account menu"
          aria-haspopup="menu"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <DotsIcon />
        </button>
      </div>

      {open ? (
        <div
          role="menu"
          className="absolute bottom-full right-0 z-20 mb-1.5 w-44 overflow-hidden rounded-xl border border-line bg-surface py-1 shadow-card"
        >
          <button
            type="button"
            role="menuitem"
            onClick={toggleTheme}
            className={cn(
              "flex w-full items-center gap-2.5 px-3 py-2 text-left text-sm text-ink transition-colors",
              "hover:bg-surface-2 focus-visible:bg-surface-2 focus-visible:outline-none",
            )}
          >
            {isDark ? <SunIcon /> : <MoonIcon />}
            <span className="flex-1">Appearance</span>
            <span className="text-[11px] text-faint">{isDark ? "Dark" : "Light"}</span>
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setOpen(false);
              onLogout();
            }}
            className={cn(
              "mt-1 flex w-full items-center gap-2.5 border-t border-line px-3 pt-2.5 pb-2 text-left text-sm text-ink transition-colors",
              "hover:bg-surface-2 focus-visible:bg-surface-2 focus-visible:outline-none",
            )}
          >
            <LogoutIcon />
            <span>Log out</span>
          </button>
        </div>
      ) : null}
    </div>
  );
}

function DotsIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-[1.15rem] w-[1.15rem]" fill="none">
      <circle cx="5" cy="12" r="1.6" fill="currentColor" />
      <circle cx="12" cy="12" r="1.6" fill="currentColor" />
      <circle cx="19" cy="12" r="1.6" fill="currentColor" />
    </svg>
  );
}

function SunIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-[1.05rem] w-[1.05rem]" fill="none">
      <circle cx="12" cy="12" r="4" stroke="currentColor" strokeWidth="1.7" />
      <path
        d="M12 2.5v2M12 19.5v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M2.5 12h2M19.5 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
      />
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-[1.05rem] w-[1.05rem]" fill="none">
      <path
        d="M20 14.2A8 8 0 1 1 9.8 4a6.4 6.4 0 0 0 10.2 10.2Z"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function LogoutIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-[1.05rem] w-[1.05rem]" fill="none">
      <path
        d="M15 12H4.5M12 8.5 15.5 12 12 15.5M9 4.5H18a1.5 1.5 0 0 1 1.5 1.5v12a1.5 1.5 0 0 1-1.5 1.5H9"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
