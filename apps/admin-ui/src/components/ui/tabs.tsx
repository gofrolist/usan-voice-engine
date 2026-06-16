import { cn } from "../../lib/cn";

export interface TabItem {
  key: string;
  label: string;
}

// Controlled vertical/horizontal tab strip. The parent owns the active key.
export function Tabs({
  items,
  active,
  onSelect,
  className,
}: {
  items: TabItem[];
  active: string;
  onSelect: (key: string) => void;
  className?: string;
}) {
  return (
    <nav className={cn("flex flex-col gap-1", className)} role="tablist">
      {items.map((it) => (
        <button
          key={it.key}
          role="tab"
          aria-selected={it.key === active}
          onClick={() => onSelect(it.key)}
          className={cn(
            "rounded-lg px-3 py-1.5 text-left text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
            it.key === active
              ? "bg-accent-soft font-medium text-accent"
              : "text-muted hover:bg-surface-2 hover:text-ink",
          )}
        >
          {it.label}
        </button>
      ))}
    </nav>
  );
}
