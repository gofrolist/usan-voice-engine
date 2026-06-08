import { Controller, type UseFormReturn } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { TOOL_NAMES, type ToolName } from "../../../config/agentConfigSchema";

const TOOL_HELP: Record<ToolName, string> = {
  log_wellness: "Record the elder's wellness response.",
  log_medication: "Log a medication as taken / not taken.",
  get_today_meds: "Read back today's medication schedule.",
  end_call: "Let the agent end the call when the check-in is complete.",
};

// Retell-style "Functions" list: each enabled tool is a function the agent can call
// mid-call. The set is fixed in Phase 1 (the registry is data-driven in a later phase).
export function ToolsSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const error = form.formState.errors.tools?.enabled?.message;
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
              {TOOL_NAMES.map((tool) => (
                <li
                  key={tool}
                  className="flex items-start justify-between gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-card"
                >
                  <label htmlFor={`tool-${tool}`} className="min-w-0">
                    <span className="font-mono text-sm text-slate-900">{tool}</span>
                    <span className="mt-0.5 block text-xs text-slate-500">{TOOL_HELP[tool]}</span>
                  </label>
                  <input
                    id={`tool-${tool}`}
                    type="checkbox"
                    className="mt-1 h-4 w-4 accent-indigo-600"
                    checked={enabled.has(tool)}
                    onChange={(e) => toggle(tool, e.target.checked)}
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
