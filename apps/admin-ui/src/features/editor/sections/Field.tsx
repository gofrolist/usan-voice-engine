import type { ReactNode } from "react";
import { fieldMeta } from "../../../config/fieldMeta";

interface FieldProps {
  // Dotted path into AgentConfig, e.g. "voice.speed". Resolves label + help from fieldMeta.
  path: string;
  error?: string;
  children: ReactNode;
  // Optional override label/help when a field is not in fieldMeta.
  label?: string;
  help?: string;
}

// Renders a labelled form row with help text and a field-level error. The control
// is supplied as children (input/textarea/select/Monaco).
export function Field({ path, error, children, label, help }: FieldProps) {
  const meta = fieldMeta[path];
  const resolvedLabel = label ?? meta?.label ?? path;
  const resolvedHelp = help ?? meta?.help;
  return (
    <div className="space-y-1">
      <label className="block text-sm font-medium text-gray-700" htmlFor={path}>
        {resolvedLabel}
      </label>
      {children}
      {resolvedHelp ? <p className="text-xs text-gray-500">{resolvedHelp}</p> : null}
      {error ? <p className="text-xs font-medium text-red-700">{error}</p> : null}
    </div>
  );
}
