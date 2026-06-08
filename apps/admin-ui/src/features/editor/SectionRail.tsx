import { cn } from "../../lib/cn";
import { SECTION_LABELS, type SectionKey } from "../../config/fieldMeta";

interface SectionRailProps {
  order: SectionKey[];
  active: SectionKey;
  summaries: Partial<Record<SectionKey, string>>;
  onSelect: (s: SectionKey) => void;
}

// Right-hand section navigation, Retell-style: each row is the section label plus an
// at-a-glance summary. The summary span is aria-hidden so each tab's accessible name
// stays exactly SECTION_LABELS[key] (keeps getByRole("tab", { name }) selectors stable).
export function SectionRail({ order, active, summaries, onSelect }: SectionRailProps) {
  return (
    <nav role="tablist" className="flex flex-col gap-1">
      {order.map((key) => (
        <button
          key={key}
          role="tab"
          aria-selected={key === active}
          onClick={() => onSelect(key)}
          className={cn(
            "flex items-center justify-between gap-2 rounded-lg px-3 py-2 text-left text-sm",
            key === active
              ? "bg-indigo-50 font-medium text-indigo-700"
              : "text-slate-600 hover:bg-slate-100",
          )}
        >
          <span>{SECTION_LABELS[key]}</span>
          {summaries[key] ? (
            <span aria-hidden="true" className="max-w-[7rem] truncate text-xs text-slate-400">
              {summaries[key]}
            </span>
          ) : null}
        </button>
      ))}
    </nav>
  );
}
