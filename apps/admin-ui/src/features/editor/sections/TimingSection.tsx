import type { UseFormReturn } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { Field } from "./Field";
import { NumberControl } from "./controls";

export function TimingSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const errors = form.formState.errors.timing;
  return (
    <div className="space-y-5">
      <Field path="timing.answer_timeout_s" error={errors?.answer_timeout_s?.message}>
        <NumberControl
          control={form.control}
          name="timing.answer_timeout_s"
          id="timing.answer_timeout_s"
          nullable={false}
          step="1"
          min={5}
          max={180}
        />
      </Field>
      <Field path="timing.max_call_duration_s" error={errors?.max_call_duration_s?.message}>
        <NumberControl
          control={form.control}
          name="timing.max_call_duration_s"
          id="timing.max_call_duration_s"
          nullable={false}
          step="1"
          min={60}
          max={7200}
        />
      </Field>
    </div>
  );
}
