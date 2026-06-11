import type { UseFormReturn } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { fieldMeta } from "../../../config/fieldMeta";
import { Field } from "./Field";
import { NumberControl, TimeControl } from "./controls";

// Per-profile policy (Phase A4, spec §6.2): quiet-hours narrowing within the
// statutory 09:00–21:00 window + bounded retry overrides. Enforced server-side at
// every consumption site — this section only edits the config. Unset state shows
// the EFFECTIVE default as a placeholder, never as a value: the UI must not write
// statutory/builtin defaults into the config (null/absent = "use the default").

// Builtin per-status chain-global attempt caps (mirror retry_policy.py ladders):
// shown as placeholders on the blank inputs.
const BUILTIN_ATTEMPT_PLACEHOLDERS = [
  ["no_answer", "2"],
  ["voicemail_left", "1"],
  ["busy", "1"],
  ["failed", "1"],
] as const;

export function PolicySection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const errors = form.formState.errors.policy;
  const attemptErrors = errors?.retry_max_attempts;
  return (
    <div className="space-y-5">
      <Field
        path="policy.quiet_hours_start_local"
        error={errors?.quiet_hours_start_local?.message}
      >
        <TimeControl
          control={form.control}
          name="policy.quiet_hours_start_local"
          id="policy.quiet_hours_start_local"
          placeholder="09:00"
        />
      </Field>
      <Field path="policy.quiet_hours_end_local" error={errors?.quiet_hours_end_local?.message}>
        <TimeControl
          control={form.control}
          name="policy.quiet_hours_end_local"
          id="policy.quiet_hours_end_local"
          placeholder="21:00"
        />
      </Field>
      <Field path="policy.retry_delay_multiplier" error={errors?.retry_delay_multiplier?.message}>
        <NumberControl
          control={form.control}
          name="policy.retry_delay_multiplier"
          id="policy.retry_delay_multiplier"
          nullable
          step="0.1"
          min={0.5}
          max={4}
          placeholder="1.0"
        />
      </Field>

      <div className="space-y-3">
        <p className="text-sm font-medium text-slate-700">
          {fieldMeta["policy.retry_max_attempts"]?.label}
        </p>
        <p className="text-xs text-slate-500">{fieldMeta["policy.retry_max_attempts"]?.help}</p>
        <div className="grid grid-cols-2 gap-4">
          {BUILTIN_ATTEMPT_PLACEHOLDERS.map(([status, placeholder]) => (
            <Field
              key={status}
              path={`policy.retry_max_attempts.${status}`}
              error={attemptErrors?.[status]?.message}
            >
              <NumberControl
                control={form.control}
                name={`policy.retry_max_attempts.${status}`}
                id={`policy.retry_max_attempts.${status}`}
                nullable
                step="1"
                min={0}
                max={4}
                placeholder={placeholder}
              />
            </Field>
          ))}
        </div>
      </div>
    </div>
  );
}
