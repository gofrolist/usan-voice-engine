import type { UseFormReturn } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { Input } from "../../../components/ui/input";
import { Field } from "./Field";
import { TextControl } from "./controls";

export function STTSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const errors = form.formState.errors.stt;
  return (
    <div className="space-y-5">
      <Field path="stt.model" error={errors?.model?.message}>
        <Input id="stt.model" {...form.register("stt.model")} />
      </Field>
      <Field path="stt.language" error={errors?.language?.message}>
        <TextControl
          control={form.control}
          name="stt.language"
          id="stt.language"
          nullable
          placeholder="(plugin default)"
        />
      </Field>
    </div>
  );
}
