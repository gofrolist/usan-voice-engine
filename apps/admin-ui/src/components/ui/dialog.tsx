import type { ReactNode } from "react";

// Minimal modal dialog: a fixed overlay + centered panel. No focus-trap library;
// Escape and backdrop click close it.
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
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onClose}
      onKeyDown={(e) => {
        if (e.key === "Escape") onClose();
      }}
      role="presentation"
    >
      <div
        className="w-full max-w-lg rounded-lg bg-white p-5 shadow-xl"
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        {title ? <h2 className="mb-3 text-lg font-semibold">{title}</h2> : null}
        {children}
      </div>
    </div>
  );
}
