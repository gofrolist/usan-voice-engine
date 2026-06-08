import { useEffect, useState } from "react";
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

// A textarea backed by a local raw-text buffer so newlines and partially-typed
// lines survive editing. The parsed string[] is pushed to the form on every change,
// but the buffer only resyncs FROM the form when the canonical array actually
// differs (e.g. a profile reset) — never mid-typing. A naive `value={arr.join("\n")}`
// re-derives the text from the trimmed/filtered array on each keystroke, which strips
// the trailing newline before a second phrase can ever be entered.
function TriggerPhrasesField({
  value,
  onChange,
}: {
  value: string[];
  onChange: (next: string[]) => void;
}) {
  const [text, setText] = useState(() => value.join("\n"));
  useEffect(() => {
    if (JSON.stringify(linesToPhrases(text)) !== JSON.stringify(value)) {
      setText(value.join("\n"));
    }
    // Resync only when the form value changes from outside (not from our own edits).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  return (
    <Textarea
      id="voicemail_detection.trigger_phrases"
      rows={4}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        onChange(linesToPhrases(e.target.value));
      }}
    />
  );
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
            <TriggerPhrasesField value={field.value} onChange={field.onChange} />
          )}
        />
      </Field>
    </div>
  );
}
