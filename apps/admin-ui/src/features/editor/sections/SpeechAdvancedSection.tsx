import { useState } from "react";
import { Controller, type UseFormReturn } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { Select } from "../../../components/ui/select";
import { Button } from "../../../components/ui/button";
import { Field } from "./Field";
import { NumberControl } from "./controls";

type SpeechErrors = UseFormReturn<AgentConfigForm>["formState"]["errors"]["speech_advanced"];

interface NumKnob {
  key: keyof AgentConfigForm["speech_advanced"];
  step: string;
  min: number;
  max: number;
}

// Every advanced knob defaults to null ("use plugin default"). Reset writes null.
const NUM_KNOBS: NumKnob[] = [
  { key: "vad_min_silence_s", step: "0.1", min: 0, max: 5 },
  { key: "vad_activation_threshold", step: "0.05", min: 0, max: 1 },
  { key: "min_endpointing_delay_s", step: "0.1", min: 0, max: 10 },
  { key: "max_endpointing_delay_s", step: "0.1", min: 0, max: 30 },
  { key: "min_interruption_duration_s", step: "0.1", min: 0, max: 5 },
  { key: "min_interruption_words", step: "1", min: 0, max: 20 },
];

export function SpeechAdvancedSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const [open, setOpen] = useState(false);
  const errors: SpeechErrors = form.formState.errors.speech_advanced;

  function resetField(key: keyof AgentConfigForm["speech_advanced"]): void {
    form.setValue(`speech_advanced.${key}`, null, {
      shouldDirty: true,
      shouldValidate: true,
    });
  }

  return (
    <div className="rounded border border-amber-200 bg-amber-50">
      <button
        type="button"
        className="flex w-full items-center justify-between px-4 py-3 text-left"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        <span className="text-sm font-medium text-amber-900">
          Advanced — can degrade call quality
        </span>
        <span className="text-amber-700">{open ? "▾" : "▸"}</span>
      </button>
      {open ? (
        <div className="space-y-5 border-t border-amber-200 bg-white p-4">
          {NUM_KNOBS.map(({ key, step, min, max }) => (
            <Field
              key={key}
              path={`speech_advanced.${key}`}
              error={errors?.[key]?.message}
            >
              <div className="flex items-center gap-2">
                <NumberControl
                  control={form.control}
                  name={`speech_advanced.${key}`}
                  id={`speech_advanced.${key}`}
                  nullable
                  step={step}
                  min={min}
                  max={max}
                />
                <Button type="button" variant="ghost" onClick={() => resetField(key)}>
                  Reset
                </Button>
              </div>
            </Field>
          ))}
          <Field
            path="speech_advanced.turn_detection"
            error={errors?.turn_detection?.message}
          >
            <div className="flex items-center gap-2">
              <Controller
                control={form.control}
                name="speech_advanced.turn_detection"
                render={({ field }) => (
                  <Select
                    id="speech_advanced.turn_detection"
                    value={field.value ?? ""}
                    onChange={(e) => field.onChange(e.target.value === "" ? null : e.target.value)}
                  >
                    <option value="">(plugin default)</option>
                    <option value="english">english</option>
                    <option value="multilingual">multilingual</option>
                    <option value="vad">vad</option>
                  </Select>
                )}
              />
              <Button
                type="button"
                variant="ghost"
                onClick={() => resetField("turn_detection")}
              >
                Reset
              </Button>
            </div>
          </Field>
        </div>
      ) : null}
    </div>
  );
}
