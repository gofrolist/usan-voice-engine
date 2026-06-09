import { Controller, type UseFormReturn } from "react-hook-form";
import type { AgentConfigForm, ToolName } from "../../../config/agentConfigSchema";
import { TOOL_NAMES } from "../../../config/agentConfigSchema";
import { useToolCatalog, type ToolSpec } from "../../../config/toolCatalog";

// Retell-style "Functions" list: each enabled tool is a function the agent can call
// mid-call. The catalog (useToolCatalog) is the runtime source of truth for what
// renders; TOOL_NAMES is the canonical order used to rebuild the enabled[] array so
// diffs stay stable. The catalog degrades gracefully: while loading or on error
// `data` is undefined, so no rows render (the form value is left untouched).
export function ToolsSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const error = form.formState.errors.tools?.enabled?.message;
  const { data: catalog } = useToolCatalog();
  // Pair each known ToolName with its catalog spec, in canonical TOOL_NAMES order and
  // restricted to names the catalog actually returned. Carrying the ToolName keeps the
  // toggle type-safe (enabled[] is ToolName[]); the spec drives the rendered labels.
  const byName = new Map<string, ToolSpec>(catalog?.map((t) => [t.name, t]) ?? []);
  const tools = TOOL_NAMES.flatMap((name) => {
    const spec = byName.get(name);
    return spec ? [{ name, spec }] : [];
  });

  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-500">Functions the agent can call during a call.</p>
      <Controller
        control={form.control}
        name="tools.enabled"
        render={({ field }) => {
          const enabled = new Set(field.value);
          function toggle(tool: ToolName, on: boolean): void {
            const next = new Set(enabled);
            if (on) next.add(tool);
            else next.delete(tool);
            // Preserve canonical order so diffs stay stable.
            field.onChange(TOOL_NAMES.filter((t) => next.has(t)));
          }
          return (
            <ul className="space-y-2">
              {tools.map(({ name, spec }) => (
                <li
                  key={name}
                  className="flex items-start justify-between gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-card"
                >
                  <label htmlFor={`tool-${name}`} className="min-w-0">
                    <span className="font-mono text-sm text-slate-900">{name}</span>
                    <span className="mt-0.5 block text-xs text-slate-500">{spec.description}</span>
                  </label>
                  <input
                    id={`tool-${name}`}
                    type="checkbox"
                    className="mt-1 h-4 w-4 accent-indigo-600"
                    checked={enabled.has(name)}
                    // always_on tools (end_call) are locked on and cannot be toggled off.
                    disabled={spec.always_on}
                    onChange={(e) => toggle(name, e.target.checked)}
                  />
                </li>
              ))}
            </ul>
          );
        }}
      />
      {error ? <p className="text-xs font-medium text-red-700">{error}</p> : null}
    </div>
  );
}
