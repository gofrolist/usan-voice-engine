import { Controller, type UseFormReturn } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { ALLOWED_TEMPLATE_SLOTS } from "../../../config/agentConfigSchema";
import { Field } from "./Field";
import { PromptEditor } from "./PromptEditor";

type PromptKey = keyof AgentConfigForm["prompts"];

const PROMPT_ORDER: PromptKey[] = [
  "system_prompt",
  "greeting",
  "recording_disclosure",
  "voicemail_message",
  "checkin_flow_instructions",
  "goodbye_message",
  "inbound_opening",
  "inbound_personalization_template",
];

// The system prompt is the editor's hero field; the long flow/template fields get
// generous height too. Everything else is a compact few-line editor.
function rowsFor(key: PromptKey): number {
  if (key === "system_prompt") return 18;
  if (key === "checkin_flow_instructions" || key === "inbound_personalization_template") return 12;
  return 4;
}

export function PromptsSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const errors = form.formState.errors.prompts;
  return (
    <div className="space-y-5">
      {PROMPT_ORDER.map((key) => {
        const path = `prompts.${key}`;
        const fieldError = errors?.[key]?.message;
        const isTemplate = key === "inbound_personalization_template";
        return (
          <Field key={key} path={path} error={fieldError}>
            <Controller
              control={form.control}
              name={`prompts.${key}`}
              render={({ field }) => (
                <PromptEditor
                  id={path}
                  value={field.value}
                  onChange={field.onChange}
                  rows={rowsFor(key)}
                />
              )}
            />
            {isTemplate ? (
              <p className="text-xs text-slate-500">
                Allowed slots:{" "}
                {ALLOWED_TEMPLATE_SLOTS.map((s) => (
                  <code key={s} className="mr-1 rounded bg-slate-100 px-1 py-0.5 font-mono">
                    {`{${s}}`}
                  </code>
                ))}
              </p>
            ) : null}
          </Field>
        );
      })}
    </div>
  );
}
