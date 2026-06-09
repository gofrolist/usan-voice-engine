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
            "rounded-lg px-3 py-1.5 text-left text-sm",
            it.key === active
              ? "bg-indigo-50 font-medium text-indigo-700"
              : "text-slate-600 hover:bg-slate-100",
          )}
        >
          {it.label}
        </button>
      ))}
    </nav>
  );
}
