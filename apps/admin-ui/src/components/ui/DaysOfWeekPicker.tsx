import type { Weekday } from "../../types/api";

const DAYS: { value: Weekday; label: string }[] = [
  { value: "monday", label: "Mon" },
  { value: "tuesday", label: "Tue" },
  { value: "wednesday", label: "Wed" },
  { value: "thursday", label: "Thu" },
  { value: "friday", label: "Fri" },
  { value: "saturday", label: "Sat" },
  { value: "sunday", label: "Sun" },
];

interface DaysOfWeekPickerProps {
  value: Weekday[];
  onChange: (days: Weekday[]) => void;
}

// Seven toggle checkboxes. Emits days in canonical Mon-first order regardless of
// click order (the server normalizes too; ordering here just avoids noisy diffs).
export function DaysOfWeekPicker({ value, onChange }: DaysOfWeekPickerProps) {
  const selected = new Set(value);
  const toggle = (day: Weekday) => {
    const next = new Set(selected);
    if (next.has(day)) next.delete(day);
    else next.add(day);
    onChange(DAYS.map((d) => d.value).filter((d) => next.has(d)));
  };
  return (
    <div className="flex flex-wrap gap-1.5">
      {DAYS.map((d) => (
        <label
          key={d.value}
          className="flex cursor-pointer items-center gap-1 rounded-lg border border-line-strong px-2 py-1 text-sm text-ink"
        >
          <input
            type="checkbox"
            aria-label={d.value}
            checked={selected.has(d.value)}
            onChange={() => toggle(d.value)}
          />
          {d.label}
        </label>
      ))}
    </div>
  );
}
