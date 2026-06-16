import { cn } from "../lib/cn";
import { dismissToast, useToasts } from "./ui/toast";

// Global toast outlet. Mutations push ApiError.detail via pushToast(); this renders
// the stack with a dismiss + (for errors) a reload affordance for 409 conflicts.
export function ErrorToast() {
  const toasts = useToasts();
  if (toasts.length === 0) return null;
  return (
    <div className="fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          role="alert"
          className={cn(
            "rounded-xl border p-3.5 text-sm shadow-pop",
            t.tone === "error"
              ? "border-red-200 bg-red-50 text-red-800"
              : "border-blue-200 bg-blue-50 text-blue-800",
          )}
        >
          <div className="flex items-start justify-between gap-2">
            <span>{t.message}</span>
            <button
              aria-label="dismiss"
              className="-mr-1 -mt-0.5 rounded p-0.5 text-lg leading-none opacity-70 transition-opacity hover:opacity-100"
              onClick={() => dismissToast(t.id)}
            >
              ×
            </button>
          </div>
          {t.tone === "error" ? (
            <button
              className="mt-2 text-xs font-semibold underline underline-offset-2"
              onClick={() => window.location.reload()}
            >
              Reload
            </button>
          ) : null}
        </div>
      ))}
    </div>
  );
}
