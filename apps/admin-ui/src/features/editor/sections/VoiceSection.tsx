import type { UseFormReturn } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { Field } from "./Field";
import { NumberControl, TextControl } from "./controls";

export function VoiceSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const errors = form.formState.errors.voice;
  return (
    <div className="space-y-5">
      <Field path="voice.cartesia_voice_id" error={errors?.cartesia_voice_id?.message}>
        <TextControl
          control={form.control}
          name="voice.cartesia_voice_id"
          id="voice.cartesia_voice_id"
          nullable
          placeholder="(plugin default)"
        />
      </Field>
      <Field path="voice.tts_model" error={errors?.tts_model?.message}>
        <TextControl
          control={form.control}
          name="voice.tts_model"
          id="voice.tts_model"
          nullable
          placeholder="(plugin default)"
        />
      </Field>
      <Field path="voice.speed" error={errors?.speed?.message}>
        <NumberControl
          control={form.control}
          name="voice.speed"
          id="voice.speed"
          nullable
          step="0.05"
          min={0.25}
          max={4.0}
        />
      </Field>
      <Field path="voice.language" error={errors?.language?.message}>
        <TextControl
          control={form.control}
          name="voice.language"
          id="voice.language"
          nullable
          placeholder="(plugin default)"
        />
      </Field>
    </div>
  );
}
