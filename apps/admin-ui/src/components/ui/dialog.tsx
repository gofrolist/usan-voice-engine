import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";

// Minimal modal dialog: a fixed overlay + centered panel. No focus-trap library;
// Escape and backdrop click close it. Rendered through a portal to document.body
// so the dialog (and any <form> it contains) is never a DOM descendant of a page
// <form> — nesting forms is invalid HTML and made a dialog's submit button submit
// the surrounding page form (e.g. inline "Declare {{var}}" reloaded the editor).
export function Dialog({
  open,
  onClose,
  title,
  children,
  closeOnBackdrop = true,
}: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children: ReactNode;
  closeOnBackdrop?: boolean;
}) {
  // Escape closes from anywhere — not only when focus already sits inside the panel
  // (the backdrop's onKeyDown only fires once focus is within the portal subtree).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && closeOnBackdrop) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose, closeOnBackdrop]);

  if (!open) return null;
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/45 p-4 backdrop-blur-sm"
      onClick={closeOnBackdrop ? onClose : undefined}
      onKeyDown={(e) => {
        if (e.key === "Escape" && closeOnBackdrop) onClose();
      }}
      role="presentation"
    >
      <div
        className="w-full max-w-lg rounded-2xl border border-line bg-surface p-6 text-ink shadow-pop"
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
        // Containment boundary: a <form> inside the dialog must not bubble its submit
        // (via the React tree, through the portal) up to a surrounding page <form>.
        onSubmit={(e) => e.stopPropagation()}
      >
        {title ? <h2 className="mb-3 font-display text-xl text-ink-strong">{title}</h2> : null}
        {children}
      </div>
    </div>,
    document.body,
  );
}
