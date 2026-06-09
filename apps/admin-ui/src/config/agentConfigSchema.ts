import { z } from "zod";

// Mirrors apps/api/src/usan_api/schemas/agent_config.py. The server is the source
// of truth; this gives instant client-side feedback with identical rules.

// Closed set of tool names the config may enable. Mirrors the backend catalog
// (apps/api/.../schemas/tool_catalog.py TOOL_CATALOG, in display order). The server
// hard-blocks names outside this set, so the zod enum below must stay in sync or a
// config that passes backend validation would fail client-side. The runtime source
// of truth for rendering is useToolCatalog (toolCatalog.ts); this static list exists
// only to give the form's zod validator identical accept/reject rules.
export const TOOL_NAMES = [
  "log_wellness",
  "log_medication",
  "get_today_meds",
  "flag_for_followup",
  "schedule_callback",
  "send_sms",
  "end_call",
] as const;
export type ToolName = (typeof TOOL_NAMES)[number];

// Personalization slots allowed in the inbound template (ALLOWED_TEMPLATE_SLOTS).
export const ALLOWED_TEMPLATE_SLOTS = ["elder_name", "last_check_in_line"] as const;

// {{name}} tokens (with optional inner whitespace) are the unified substitution
// syntax. Mirrors apps/api TOKEN_RE / the agent's prompt_vars.TOKEN_RE.
const DOUBLE_TOKEN_RE = /\{\{\s*[a-zA-Z0-9_]+\s*\}\}/g;

// PHI built-in variable names (mirror apps/api PHI_BUILTIN_NAMES). An SMS template
// body referencing any of these hard-blocks (design §6.2) — stricter than greetings.
const PHI_TOKEN_NAMES = [
  "last_check_in",
  "last_check_in_line",
  "last_mood",
  "last_pain",
  "today_meds",
] as const;
// {{name}} token capture (mirrors DOUBLE_TOKEN_RE but captures the name).
const TOKEN_NAME_RE = /\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g;

export const smsTemplateSchema = z
  .object({
    key: z
      .string()
      .min(1)
      .max(64)
      .regex(/^[a-z0-9_]+$/, "key must be a lowercase slug (a-z, 0-9, _)"),
    label: z.string().min(1).max(120),
    body: z.string().min(1).max(480),
  })
  .superRefine((v, ctx) => {
    const phi = new Set<string>(PHI_TOKEN_NAMES);
    for (const m of v.body.matchAll(TOKEN_NAME_RE)) {
      const name = m[1];
      if (name !== undefined && phi.has(name)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["body"],
          message: `SMS body must not reference protected health information ({{${name}}})`,
        });
      }
    }
  });
// Legacy single-brace slots kept only for back-compat in the personalization template.
const LEGACY_SLOT_RE = /\{(elder_name|last_check_in_line)\}/g;

// Field-tiered brace rule (mirrors apps/api schemas/agent_config.py, spec §5.1):
// strip the allowed {{tokens}} (and, for the template, the legacy {slots}) and if any
// lone '{' or '}' remains it is a typo -> reject. Unknown {{var}} NAMES are never
// rejected here (warn-only, surfaced in the editor from the fetched catalog).
function rejectStrayBraces(label: string, allowLegacySlots = false) {
  return (v: string, ctx: z.RefinementCtx) => {
    let stripped = v.replace(DOUBLE_TOKEN_RE, "");
    if (allowLegacySlots) stripped = stripped.replace(LEGACY_SLOT_RE, "");
    if (stripped.includes("{") || stripped.includes("}")) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: `${label} has a stray '{' or '}' (use {{variable}} tokens)`,
      });
    }
  };
}

// allowBraces=true: permissive big fields (system_prompt, checkin_flow_instructions) —
// any braces allowed, unchanged from Phase 1, since substitution is token-scoped.
function promptField(maxLength: number, label: string, allowBraces = false) {
  const base = z
    .string()
    .min(1, `${label} is required`)
    .max(maxLength, `${label} must be at most ${maxLength} characters`);
  return allowBraces ? base : base.superRefine(rejectStrayBraces(label));
}

// inbound_personalization_template: allow {{tokens}} PLUS the two legacy single-brace
// slots; reject any other stray brace.
const personalizationTemplate = z
  .string()
  .min(1, "Personalization template is required")
  .max(6000, "Personalization template must be at most 6000 characters")
  .superRefine(rejectStrayBraces("Personalization template", true));

export const promptsSchema = z.object({
  // Permissive big fields: any braces allowed (hold {{variable}} tokens + arbitrary
  // pasted braces; never str.format-ed). Mirrors apps/api PromptsConfig.
  system_prompt: promptField(24000, "System prompt", true),
  // Short fields: allow {{tokens}}, reject a lone stray brace.
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
  sms: z.object({ templates: z.array(smsTemplateSchema) }).optional().nullable(),
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
