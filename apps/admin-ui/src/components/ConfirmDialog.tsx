import type { ReactNode } from "react";
import { Dialog } from "./ui/dialog";
import { Button } from "./ui/button";

// Reusable confirm modal for destructive actions (archive, rollback, remove admin).
// The caller surfaces server-guard errors (e.g. 409 last-admin) via the toast store.
export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = "Confirm",
  confirmVariant = "danger",
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  confirmVariant?: "primary" | "danger";
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Dialog open={open} onClose={onCancel} title={title}>
      <div className="text-sm text-slate-700">{body}</div>
      <div className="mt-5 flex justify-end gap-2">
        <Button variant="secondary" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
        <Button variant={confirmVariant} onClick={onConfirm} disabled={busy}>
          {confirmLabel}
        </Button>
      </div>
    </Dialog>
  );
}
