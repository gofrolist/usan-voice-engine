import { useId } from "react";
import { Input } from "./input";
import { Button } from "./button";

// A row keeps its own identity so editing a key never reorders/loses focus. The
// emitted record contains only rows with a non-empty (trimmed) key; a later
// duplicate key wins, matching plain-object semantics.
export interface KvRow {
  id: string;
  key: string;
  value: string;
}

export function rowsToRecord(rows: KvRow[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of rows) {
    const k = r.key.trim();
    if (k.length > 0) out[k] = r.value;
  }
  return out;
}

export function recordToRows(record: Record<string, unknown>): KvRow[] {
  return Object.entries(record).map(([key, value], i) => ({
    id: `r${i}-${key}`,
    key,
    value: value == null ? "" : String(value),
  }));
}

interface KeyValueEditorProps {
  rows: KvRow[];
  onChange: (rows: KvRow[]) => void;
  label: string;
  addLabel?: string;
}

export function KeyValueEditor({
  rows,
  onChange,
  label,
  addLabel = "Add variable",
}: KeyValueEditorProps) {
  const baseId = useId();
  const setRow = (id: string, patch: Partial<KvRow>) =>
    onChange(rows.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  const addRow = () =>
    onChange([...rows, { id: `${baseId}-${rows.length}-${Date.now()}`, key: "", value: "" }]);
  const removeRow = (id: string) => onChange(rows.filter((r) => r.id !== id));

  return (
    <div>
      <div className="mb-1 block text-xs font-medium text-slate-600">{label}</div>
      <div className="space-y-2">
        {rows.map((r, i) => (
          <div key={r.id} className="flex items-center gap-2">
            <Input
              aria-label={`${label} key ${i + 1}`}
              placeholder="first_name"
              value={r.key}
              onChange={(e) => setRow(r.id, { key: e.target.value })}
            />
            <Input
              aria-label={`${label} value ${i + 1}`}
              placeholder="Jane"
              value={r.value}
              onChange={(e) => setRow(r.id, { value: e.target.value })}
            />
            <Button
              type="button"
              variant="ghost"
              aria-label={`Remove ${r.key || "row"}`}
              onClick={() => removeRow(r.id)}
            >
              ✕
            </Button>
          </div>
        ))}
      </div>
      <Button type="button" variant="secondary" className="mt-2" onClick={addRow}>
        {addLabel}
      </Button>
    </div>
  );
}
