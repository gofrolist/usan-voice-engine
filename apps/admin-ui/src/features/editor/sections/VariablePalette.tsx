import { useState } from "react";
import { Badge } from "../../../components/ui/badge";
import { groupByTier, type VariableSpec } from "../../../config/variableCatalog";

interface VariablePaletteProps {
  variables: VariableSpec[];
  // Receives the ready-to-insert token, e.g. "{{first_name}}".
  onInsert: (token: string) => void;
}

const TIER_LABELS: { key: "builtin" | "custom"; label: string }[] = [
  { key: "builtin", label: "Built-in" },
  { key: "custom", label: "Custom" },
];

// Retell-style "insert variable" control: a {} button that opens a grouped list of
// catalog variables; clicking one inserts {{name}} at the editor cursor (the parent
// wires onInsert to a Monaco executeEdits).
export function VariablePalette({ variables, onInsert }: VariablePaletteProps) {
  const [open, setOpen] = useState(false);
  const groups = groupByTier(variables);

  function pick(name: string): void {
    onInsert(`{{${name}}}`);
    setOpen(false);
  }

  return (
    <div className="relative inline-block">
      <button
        type="button"
        aria-label="Insert variable"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="rounded border border-slate-300 bg-white px-2 py-1 font-mono text-xs text-slate-600 hover:bg-slate-50"
      >
        {"{ }"}
      </button>
      {open ? (
        <div className="absolute z-10 mt-1 max-h-72 w-72 overflow-auto rounded-lg border border-slate-200 bg-white p-2 shadow-lg">
          {TIER_LABELS.map(({ key, label }) =>
            groups[key].length === 0 ? null : (
              <div key={key} className="mb-2 last:mb-0">
                <p className="px-1 py-0.5 text-xs font-semibold uppercase tracking-wide text-slate-400">
                  {label}
                </p>
                {groups[key].map((v) => (
                  <button
                    key={v.name}
                    type="button"
                    onClick={() => pick(v.name)}
                    className="block w-full rounded px-1 py-1 text-left hover:bg-indigo-50"
                  >
                    <code className="font-mono text-xs text-indigo-700">{`{{${v.name}}}`}</code>
                    {v.phi ? (
                      <span className="ml-2">
                        <Badge tone="red">PHI</Badge>
                      </span>
                    ) : null}
                    <span className="ml-2 text-xs text-slate-500">{v.description}</span>
                  </button>
                ))}
              </div>
            ),
          )}
        </div>
      ) : null}
    </div>
  );
}
