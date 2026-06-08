import { cn } from "../../lib/cn";

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      role="status"
      aria-label="loading"
      className={cn(
        "inline-block h-5 w-5 animate-spin rounded-full border-2 border-gray-300 border-t-blue-600",
        className,
      )}
    />
  );
}
