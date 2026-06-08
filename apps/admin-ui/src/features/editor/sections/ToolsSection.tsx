import { Controller, type UseFormReturn } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { TOOL_NAMES, type ToolName } from "../../../config/agentConfigSchema";
import { fieldMeta } from "../../../config/fieldMeta";

const TOOL_HELP: Record<ToolName, string> = {
  log_wellness: "Record the elder's wellness response.",
  log_medication: "Log a medication as taken / not taken.",
  get_today_meds: "Read back today's medication schedule.",
  end_call: "Let the agent end the call when the check-in is complete.",
};

export function ToolsSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const meta = fieldMeta["tools.enabled"];
  const error = form.formState.errors.tools?.enabled?.message;
  return (
    <div className="space-y-3">
      <div>
        <p className="text-sm font-medium text-gray-700">{meta?.label}</p>
        {meta?.help ? <p className="text-xs text-gray-500">{meta.help}</p> : null}
      </div>
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
                <li key={tool} className="flex items-start gap-2">
                  <input
                    id={`tool-${tool}`}
                    type="checkbox"
                    className="mt-0.5"
                    checked={enabled.has(tool)}
                    onChange={(e) => toggle(tool, e.target.checked)}
                  />
                  <label htmlFor={`tool-${tool}`} className="text-sm">
                    <span className="font-mono">{tool}</span>
                    <span className="ml-2 text-gray-500">{TOOL_HELP[tool]}</span>
                  </label>
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
