import { useEffect, useRef, useState } from "react";
import { Outlet } from "react-router-dom";
import { NavSidebar, SidebarNav } from "./NavSidebar";
import { ActingAsBanner } from "./ActingAsBanner";
import { ErrorToast } from "./ErrorToast";
import { ThemeToggle } from "./ui/ThemeToggle";

function MenuIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" className="h-5 w-5" fill="none">
      <path d="M4 7h16M4 12h16M4 17h16" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

// Shell rendered for every authenticated route. The frame itself does NOT scroll —
// each routed view owns its scroll (simple pages via PageLayout; the editor via its
// own full-height panes). On desktop the sidebar is a persistent rail; on mobile it
// collapses behind a top-bar menu button into a slide-over drawer. Wrapped by RequireAuth.
export function AppLayout() {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const drawerRef = useRef<HTMLElement>(null);
  const menuButtonRef = useRef<HTMLButtonElement>(null);

  // Enforce the modality the drawer's aria-modal promises: move focus in on open,
  // restore it to the menu button on close, trap Tab inside, close on Escape, and
  // lock body scroll. Hand-rolled (no headless dep) but covers the keyboard/AT path.
  useEffect(() => {
    if (!drawerOpen) return;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    // Snapshot the trigger now so the cleanup restores focus to a stable reference.
    const menuButton = menuButtonRef.current;
    drawerRef.current?.focus();
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    function onKeyDown(e: KeyboardEvent): void {
      if (e.key === "Escape") {
        setDrawerOpen(false);
        return;
      }
      if (e.key !== "Tab" || !drawerRef.current) return;
      const focusables = drawerRef.current.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
      );
      if (focusables.length === 0) return;
      const first = focusables[0]!;
      const last = focusables[focusables.length - 1]!;
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = prevOverflow;
      (previouslyFocused ?? menuButton)?.focus();
    };
  }, [drawerOpen]);

  return (
    <div className="flex h-screen overflow-hidden bg-canvas text-ink">
      <NavSidebar />

      {/* Mobile slide-over drawer (md:hidden). */}
      {drawerOpen ? (
        <div className="fixed inset-0 z-40 md:hidden">
          <div
            className="absolute inset-0 bg-black/45 backdrop-blur-sm"
            onClick={() => setDrawerOpen(false)}
            role="presentation"
          />
          <aside
            ref={drawerRef}
            tabIndex={-1}
            className="absolute inset-y-0 left-0 w-64 max-w-[82%] border-r border-line bg-surface shadow-pop focus:outline-none"
            role="dialog"
            aria-modal="true"
            aria-label="Navigation"
          >
            <SidebarNav onNavigate={() => setDrawerOpen(false)} />
          </aside>
        </div>
      ) : null}

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        {/* Mobile top bar. */}
        <header className="flex shrink-0 items-center justify-between gap-2 border-b border-line bg-surface px-4 py-2.5 md:hidden">
          <button
            ref={menuButtonRef}
            type="button"
            aria-label="Open navigation menu"
            onClick={() => setDrawerOpen(true)}
            className="inline-flex h-9 w-9 items-center justify-center rounded-lg border border-line text-muted transition-colors hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <MenuIcon />
          </button>
          <span className="flex items-center gap-2">
            <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-ink text-xs font-bold text-canvas">
              U
            </span>
            <span className="font-display text-base font-semibold text-ink-strong">USAN Admin</span>
          </span>
          <ThemeToggle />
        </header>

        <ActingAsBanner />
        <main className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
          <Outlet />
        </main>
      </div>
      <ErrorToast />
    </div>
  );
}
