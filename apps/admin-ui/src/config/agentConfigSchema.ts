import { z } from "zod";

// Mirrors apps/api/src/usan_api/schemas/agent_config.py. The server is the source
// of truth; this gives instant client-side feedback with identical rules.

// Tool names the agent can register (TOOL_NAMES).
export const TOOL_NAMES = ["log_wellness", "log_medication", "get_today_meds", "end_call"] as const;
export type ToolName = (typeof TOOL_NAMES)[number];

// Personalization slots allowed in the inbound template (ALLOWED_TEMPLATE_SLOTS).
export const ALLOWED_TEMPLATE_SLOTS = ["elder_name", "last_check_in_line"] as const;

const SLOT_RE = /\{([^{}]*)\}/g;

// Reject raw format-slot braces on every prompt except the personalization template.
function noBraces(label: string) {
  return (v: string, ctx: z.RefinementCtx) => {
    if (v.includes("{") || v.includes("}")) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: `${label} must not contain '{' or '}'`,
      });
    }
  };
}

function promptField(maxLength: number, label: string, allowBraces = false) {
  const base = z
    .string()
    .min(1, `${label} is required`)
    .max(maxLength, `${label} must be at most ${maxLength} characters`);
  // system_prompt and checkin_flow_instructions hold {{variable}} tokens for migrated
  // prompts and are never str.format-ed on the agent, so they skip the brace check.
  return allowBraces ? base : base.superRefine(noBraces(label));
}

// inbound_personalization_template: allow ONLY {elder_name} and {last_check_in_line}.
const personalizationTemplate = z
  .string()
  .min(1, "Personalization template is required")
  .max(6000, "Personalization template must be at most 6000 characters")
  .superRefine((v, ctx) => {
    const slots: string[] = [];
    for (const m of v.matchAll(SLOT_RE)) {
      if (m[1] !== undefined) slots.push(m[1]);
    }
    const allowed: readonly string[] = ALLOWED_TEMPLATE_SLOTS;
    const bad = [...new Set(slots.filter((s) => !allowed.includes(s)))].sort();
    if (bad.length > 0) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: `unknown template slot(s): ${bad.join(", ")}; allowed: ${[...ALLOWED_TEMPLATE_SLOTS].join(", ")}`,
      });
    }
    // Reject stray braces not part of a recognized slot.
    const stripped = v.replace(SLOT_RE, "");
    if (stripped.includes("{") || stripped.includes("}")) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "contains an unmatched '{' or '}'",
      });
    }
  });

export const promptsSchema = z.object({
  // Large free-form behavior fields: braces allowed (hold {{variable}} tokens; never
  // str.format-ed). Mirrors apps/api PromptsConfig.
  system_prompt: promptField(24000, "System prompt", true),
  greeting: promptField(1000, "Greeting"),
  recording_disclosure: promptField(1000, "Recording disclosure"),
  voicemail_message: promptField(1000, "Voicemail message"),
  checkin_flow_instructions: promptField(24000, "Check-in flow instructions", true),
  goodbye_message: promptField(1000, "Goodbye message"),
  inbound_opening: promptField(1000, "Inbound opening"),
  inbound_personalization_template: personalizationTemplate,
});

// Optional string with min/max length when present, nullable.
function optStr(min: number, max: number) {
  return z.string().min(min).max(max).nullable();
}

export const voiceSchema = z.object({
  cartesia_voice_id: optStr(1, 200),
  tts_model: optStr(1, 100),
  speed: z.number().gte(0.25).lte(4.0).nullable(),
  language: optStr(1, 20),
});

export const llmSchema = z.object({
  model: z.string().min(1).max(200),
  temperature: z.number().gte(0.0).lte(2.0).nullable(),
});

export const sttSchema = z.object({
  model: z.string().min(1).max(200),
  language: optStr(1, 20),
});

export const timingSchema = z.object({
  answer_timeout_s: z.number().gte(5.0).lte(180.0),
  max_call_duration_s: z.number().int().gte(60).lte(7200),
});

export const toolsSchema = z.object({
  enabled: z.array(z.enum(TOOL_NAMES)),
});

export const voicemailDetectionSchema = z.object({
  window_s: z.number().gte(0.5).lte(30.0),
  trigger_phrases: z.array(z.string()),
});

export const speechAdvancedSchema = z
  .object({
    vad_min_silence_s: z.number().gte(0.0).lte(5.0).nullable(),
    vad_activation_threshold: z.number().gte(0.0).lte(1.0).nullable(),
    turn_detection: z.enum(["english", "multilingual", "vad"]).nullable(),
    min_endpointing_delay_s: z.number().gte(0.0).lte(10.0).nullable(),
    max_endpointing_delay_s: z.number().gte(0.0).lte(30.0).nullable(),
    min_interruption_duration_s: z.number().gte(0.0).lte(5.0).nullable(),
    min_interruption_words: z.number().int().gte(0).lte(20).nullable(),
  })
  .superRefine((v, ctx) => {
    const mn = v.min_endpointing_delay_s;
    const mx = v.max_endpointing_delay_s;
    if (mn !== null && mx !== null && mn > mx) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["min_endpointing_delay_s"],
        message: `min_endpointing_delay_s (${mn}) must be <= max_endpointing_delay_s (${mx})`,
      });
    }
  });

export const agentConfigSchema = z.object({
  prompts: promptsSchema,
  voice: voiceSchema,
  llm: llmSchema,
  stt: sttSchema,
  timing: timingSchema,
  tools: toolsSchema,
  voicemail_detection: voicemailDetectionSchema,
  speech_advanced: speechAdvancedSchema,
});

export type AgentConfigForm = z.infer<typeof agentConfigSchema>;
