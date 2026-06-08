import { Controller, type UseFormReturn } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { Textarea } from "../../../components/ui/textarea";
import { Field } from "./Field";
import { NumberControl } from "./controls";

// trigger_phrases is edited as one phrase per line; empty lines are dropped.
function linesToPhrases(text: string): string[] {
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
}

export function VoicemailSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const errors = form.formState.errors.voicemail_detection;
  return (
    <div className="space-y-5">
      <Field path="voicemail_detection.window_s" error={errors?.window_s?.message}>
        <NumberControl
          control={form.control}
          name="voicemail_detection.window_s"
          id="voicemail_detection.window_s"
          nullable={false}
          step="0.5"
          min={0.5}
          max={30}
        />
      </Field>
      <Field
        path="voicemail_detection.trigger_phrases"
        error={errors?.trigger_phrases?.message}
        help="One phrase per line. Empty = built-in patterns."
      >
        <Controller
          control={form.control}
          name="voicemail_detection.trigger_phrases"
          render={({ field }) => (
            <Textarea
              id="voicemail_detection.trigger_phrases"
              rows={4}
              value={field.value.join("\n")}
              onChange={(e) => field.onChange(linesToPhrases(e.target.value))}
            />
          )}
        />
      </Field>
    </div>
  );
}
