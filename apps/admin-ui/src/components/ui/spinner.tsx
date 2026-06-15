import { cn } from "../../lib/cn";

export function Spinner({ className }: { className?: string }) {
  return (
    <span
      role="status"
      aria-label="loading"
      className={cn(
        "inline-block h-5 w-5 animate-spin rounded-full border-2 border-line-strong border-t-accent",
        className,
      )}
    />
  );
}
