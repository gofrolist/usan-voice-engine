import { cn } from "../../lib/cn";
import { SECTION_LABELS, type SectionKey } from "../../config/fieldMeta";

interface SectionRailProps {
  order: SectionKey[];
  active: SectionKey;
  summaries: Partial<Record<SectionKey, string>>;
  onSelect: (s: SectionKey) => void;
}

// Section navigation, Retell-style: each row is the section label plus an at-a-glance
// summary. On desktop it is the vertical right rail; on mobile it reflows into a single
// horizontal scrolling strip (one instance either way — keeps the tab roles unique). The
// summary span is aria-hidden so each tab's accessible name stays exactly
// SECTION_LABELS[key] (keeps getByRole("tab", { name }) selectors stable).
export function SectionRail({ order, active, summaries, onSelect }: SectionRailProps) {
  return (
    <nav role="tablist" className="flex gap-1 md:flex-col">
      {order.map((key) => (
        <button
          key={key}
          role="tab"
          aria-selected={key === active}
          onClick={() => onSelect(key)}
          className={cn(
            "flex shrink-0 items-center justify-between gap-2 whitespace-nowrap rounded-lg px-3 py-2 text-left text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent md:w-full md:shrink",
            key === active
              ? "bg-accent-soft font-medium text-accent"
              : "text-muted hover:bg-surface-2 hover:text-ink",
          )}
        >
          <span>{SECTION_LABELS[key]}</span>
          {summaries[key] ? (
            <span
              aria-hidden="true"
              className="hidden max-w-[7rem] truncate text-xs text-faint md:inline"
            >
              {summaries[key]}
            </span>
          ) : null}
        </button>
      ))}
    </nav>
  );
}
