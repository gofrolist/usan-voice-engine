import type { ReactNode } from "react";
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
}: {
  open: boolean;
  onClose: () => void;
  title?: ReactNode;
  children: ReactNode;
}) {
  if (!open) return null;
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
      onKeyDown={(e) => {
        if (e.key === "Escape") onClose();
      }}
      role="presentation"
    >
      <div
        className="w-full max-w-lg rounded-xl border border-slate-200 bg-white p-5 shadow-xl"
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
        // Containment boundary: a <form> inside the dialog must not bubble its submit
        // (via the React tree, through the portal) up to a surrounding page <form>.
        onSubmit={(e) => e.stopPropagation()}
      >
        {title ? <h2 className="mb-3 text-lg font-semibold">{title}</h2> : null}
        {children}
      </div>
    </div>,
    document.body,
  );
}
