import type { UseFormReturn } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { Input } from "../../../components/ui/input";
import { Field } from "./Field";
import { NumberControl } from "./controls";

export function LLMSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const errors = form.formState.errors.llm;
  return (
    <div className="space-y-5">
      <Field path="llm.model" error={errors?.model?.message}>
        <Input id="llm.model" {...form.register("llm.model")} />
      </Field>
      <Field path="llm.temperature" error={errors?.temperature?.message}>
        <NumberControl
          control={form.control}
          name="llm.temperature"
          id="llm.temperature"
          nullable
          step="0.1"
          min={0}
          max={2}
        />
      </Field>
    </div>
  );
}
