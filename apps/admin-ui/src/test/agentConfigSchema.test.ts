import { describe, expect, it } from "vitest";
import {
  agentConfigSchema,
  policySchema,
  smsTemplateSchema,
  TOOL_NAMES,
  toolsSchema,
  type AgentConfigForm,
} from "../config/agentConfigSchema";

function validConfig(): AgentConfigForm {
  return {
    prompts: {
      system_prompt: "You are a warm check-in assistant.",
      greeting: "Hello, how are you today?",
      recording_disclosure: "This call is recorded.",
      voicemail_message: "Sorry we missed you.",
      checkin_flow_instructions: "Ask how they feel, then meds.",
      goodbye_message: "Take care, goodbye.",
      inbound_opening: "Greet warmly and begin.",
      inbound_personalization_template:
        "Speaking with {contact_name}. {last_check_in_line} Begin the check-in.",
    },
    voice: {
      cartesia_voice_id: null,
      tts_model: null,
      speed: null,
      language: null,
    },
    llm: { model: "gemini-3.1-flash-lite", temperature: null },
    stt: { model: "ink-whisper", language: null },
    timing: { answer_timeout_s: 50, max_call_duration_s: 1800 },
    tools: {
      enabled: [
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "flag_for_followup",
        "schedule_callback",
        "send_sms",
        "end_call",
      ],
    },
    voicemail_detection: { window_s: 3, trigger_phrases: [] },
    speech_advanced: {
      vad_min_silence_s: null,
      vad_activation_threshold: null,
      turn_detection: null,
      min_endpointing_delay_s: null,
      max_endpointing_delay_s: null,
      min_interruption_duration_s: null,
      min_interruption_words: null,
    },
  };
}

describe("agentConfigSchema", () => {
  it("accepts a valid config", () => {
    expect(agentConfigSchema.safeParse(validConfig()).success).toBe(true);
  });

  it("exposes all seven catalog tool names", () => {
    expect([...TOOL_NAMES]).toEqual([
      "log_wellness",
      "log_medication",
      "get_today_meds",
      "flag_for_followup",
      "schedule_callback",
      "send_sms",
      "end_call",
    ]);
  });

  it("rejects a brace in the greeting", () => {
    const cfg = validConfig();
    cfg.prompts.greeting = "Hello {name}, how are you?";
    expect(agentConfigSchema.safeParse(cfg).success).toBe(false);
  });

  it("accepts an unknown single-brace slot in the template (permissive field)", () => {
    const cfg = validConfig();
    cfg.prompts.inbound_personalization_template = "Hello {first_name}, welcome.";
    expect(agentConfigSchema.safeParse(cfg).success).toBe(true);
  });

  it("rejects voice.speed = 5 (above 4.0)", () => {
    const cfg = validConfig();
    cfg.voice.speed = 5;
    expect(agentConfigSchema.safeParse(cfg).success).toBe(false);
  });

  it("rejects min_endpointing_delay > max_endpointing_delay", () => {
    const cfg = validConfig();
    cfg.speech_advanced.min_endpointing_delay_s = 5;
    cfg.speech_advanced.max_endpointing_delay_s = 2;
    expect(agentConfigSchema.safeParse(cfg).success).toBe(false);
  });

  it("rejects an unknown tool", () => {
    const cfg = validConfig();
    // @ts-expect-error intentionally invalid tool name for the test
    cfg.tools.enabled = ["log_wellness", "do_a_barrel_roll"];
    expect(agentConfigSchema.safeParse(cfg).success).toBe(false);
  });

  it("accepts a long system_prompt containing {{braces}}", () => {
    const cfg = validConfig();
    cfg.prompts.system_prompt = "You are Clara. Greet {{first_name}}.\n".repeat(300).slice(0, 12000);
    expect(agentConfigSchema.safeParse(cfg).success).toBe(true);
  });

  it("accepts braces in checkin_flow_instructions", () => {
    const cfg = validConfig();
    cfg.prompts.checkin_flow_instructions = "Ask about {{med_name}} at {{time}}.";
    expect(agentConfigSchema.safeParse(cfg).success).toBe(true);
  });

  it("rejects a system_prompt over 24000 chars", () => {
    const cfg = validConfig();
    cfg.prompts.system_prompt = "x".repeat(24001);
    expect(agentConfigSchema.safeParse(cfg).success).toBe(false);
  });

  it("accepts a {{token}} in the greeting (short field)", () => {
    const cfg = validConfig();
    cfg.prompts.greeting = "Hello {{first_name}}, how are you?";
    expect(agentConfigSchema.safeParse(cfg).success).toBe(true);
  });

  it("accepts an UNKNOWN {{token}} in the greeting (warn, never block)", () => {
    const cfg = validConfig();
    cfg.prompts.greeting = "Hello {{totally_made_up}}, welcome.";
    expect(agentConfigSchema.safeParse(cfg).success).toBe(true);
  });

  it("rejects a stray single brace in the greeting", () => {
    const cfg = validConfig();
    cfg.prompts.greeting = "Hello {first_name}, how are you?";
    expect(agentConfigSchema.safeParse(cfg).success).toBe(false);
  });

  it("rejects a lone unmatched brace in the voicemail_message", () => {
    const cfg = validConfig();
    cfg.prompts.voicemail_message = "Sorry we missed you }";
    expect(agentConfigSchema.safeParse(cfg).success).toBe(false);
  });

  it("accepts {{tokens}} in the personalization template", () => {
    const cfg = validConfig();
    cfg.prompts.inbound_personalization_template =
      "Speaking with {{contact_name}}. {{last_check_in_line}} Begin.";
    expect(agentConfigSchema.safeParse(cfg).success).toBe(true);
  });

  it("still accepts the two legacy single-brace slots in the template", () => {
    const cfg = validConfig();
    cfg.prompts.inbound_personalization_template =
      "Speaking with {contact_name}. {last_check_in_line} Begin.";
    expect(agentConfigSchema.safeParse(cfg).success).toBe(true);
  });

  it("accepts a stray lone brace in the template (no 500 on read)", () => {
    const cfg = validConfig();
    cfg.prompts.inbound_personalization_template = "Speaking with {contact_name} and {";
    expect(agentConfigSchema.safeParse(cfg).success).toBe(true);
  });

  it("accepts an unknown {{token}} in the template (warn, never block)", () => {
    const cfg = validConfig();
    cfg.prompts.inbound_personalization_template =
      "Speaking with {{contact_name}}. {{made_up_var}} Begin.";
    expect(agentConfigSchema.safeParse(cfg).success).toBe(true);
  });
});

describe("smsTemplateSchema", () => {
  it("accepts a non-PHI body", () => {
    const r = smsTemplateSchema.safeParse({
      key: "med_reminder",
      label: "Med reminder",
      body: "Hi {{first_name}}, reminder for {{current_date}}.",
    });
    expect(r.success).toBe(true);
  });

  it("rejects a non-slug key", () => {
    const r = smsTemplateSchema.safeParse({ key: "Bad Key", label: "x", body: "hi" });
    expect(r.success).toBe(false);
  });

  it.each(["last_check_in", "last_check_in_line", "last_mood", "last_pain", "today_meds"])(
    "hard-blocks PHI token %s in the body",
    (token) => {
      const r = smsTemplateSchema.safeParse({
        key: "k",
        label: "L",
        body: `Your status: {{${token}}}`,
      });
      expect(r.success).toBe(false);
    },
  );

  it("toolsSchema accepts an sms block with templates", () => {
    const r = toolsSchema.safeParse({
      enabled: ["log_wellness", "send_sms", "end_call"],
      sms: { templates: [{ key: "k", label: "L", body: "Hi {{first_name}}" }] },
    });
    expect(r.success).toBe(true);
  });

  it("toolsSchema sms is optional", () => {
    const r = toolsSchema.safeParse({ enabled: ["end_call"] });
    expect(r.success).toBe(true);
  });
});

describe("policySchema", () => {
  it("policySchema mirrors pydantic bounds", () => {
    // Statutory widening rejected (mirror of the server's _narrowing_only gate).
    expect(policySchema.safeParse({ quiet_hours_start_local: "08:59" }).success).toBe(false);
    expect(policySchema.safeParse({ quiet_hours_end_local: "21:01" }).success).toBe(false);
    // start >= end rejected, with the issue parked on the start field.
    const crossed = policySchema.safeParse({
      quiet_hours_start_local: "12:00",
      quiet_hours_end_local: "10:00",
    });
    expect(crossed.success).toBe(false);
    if (!crossed.success) {
      expect(crossed.error.issues.some((i) => i.path[0] === "quiet_hours_start_local")).toBe(true);
    }
    // HH:MM regex: 24-hour, zero-padded — single-digit hour is a format error.
    expect(policySchema.safeParse({ quiet_hours_start_local: "9:00" }).success).toBe(false);
    // retry_delay_multiplier bounds 0.5–4.0 (PolicyConfig ge/le mirror).
    expect(policySchema.safeParse({ retry_delay_multiplier: 0.4 }).success).toBe(false);
    expect(policySchema.safeParse({ retry_delay_multiplier: 4.1 }).success).toBe(false);
    // retry_max_attempts.* bounds 0–4 (RetryMaxAttempts ge/le mirror).
    expect(policySchema.safeParse({ retry_max_attempts: { busy: 5 } }).success).toBe(false);
    expect(policySchema.safeParse({ retry_max_attempts: { busy: -1 } }).success).toBe(false);
    // One-sided narrowing OK (the unset side stays statutory), minute granularity.
    expect(policySchema.safeParse({ quiet_hours_start_local: "09:30" }).success).toBe(true);
    expect(policySchema.safeParse({ quiet_hours_end_local: "20:45" }).success).toBe(true);
    // Exact statutory boundaries (mirror of the pydantic rows): >= 09:00 and
    // <= 21:00 are INCLUSIVE, so restating a statutory edge is a valid no-op...
    expect(policySchema.safeParse({ quiet_hours_start_local: "09:00" }).success).toBe(true);
    expect(policySchema.safeParse({ quiet_hours_end_local: "21:00" }).success).toBe(true);
    expect(
      policySchema.safeParse({
        quiet_hours_start_local: "09:00",
        quiet_hours_end_local: "21:00",
      }).success,
    ).toBe(true);
    // ...but start at the END boundary leaves an empty effective window
    // (start 21:00 >= effective end 21:00) — rejected by start < end.
    expect(policySchema.safeParse({ quiet_hours_start_local: "21:00" }).success).toBe(false);
    expect(
      policySchema.safeParse({
        quiet_hours_start_local: "21:00",
        quiet_hours_end_local: "21:00",
      }).success,
    ).toBe(false);
  });

  it("empty time input transforms to null", () => {
    // A cleared <input type="time"> yields "" (never null) — pristine forms must
    // validate, so the schema turns "" into null before the HH:MM regex (spec §6.2).
    const parsed = policySchema.parse({
      quiet_hours_start_local: "",
      quiet_hours_end_local: "",
      retry_delay_multiplier: null,
      retry_max_attempts: null,
    });
    expect(parsed.quiet_hours_start_local).toBeNull();
    expect(parsed.quiet_hours_end_local).toBeNull();
  });

  it("older draft without policy resets cleanly", () => {
    // validConfig() carries no `policy` key — the shape of every pre-A4 draft that
    // form.reset(profile.draft_config) must accept (.optional().nullable(), shaped
    // like toolsSchema.sms).
    expect(agentConfigSchema.safeParse(validConfig()).success).toBe(true);
    expect(agentConfigSchema.safeParse({ ...validConfig(), policy: null }).success).toBe(true);
  });

  it("policy object paths match pydantic field names", () => {
    // Server 422 locs are ["body","config","policy",<field...>]; mapServerErrors
    // slices the envelope positionally, so zod issue paths must equal the pydantic
    // field names exactly.
    const badStart = policySchema.safeParse({ quiet_hours_start_local: "9:00" });
    expect(badStart.success).toBe(false);
    if (!badStart.success) {
      expect(badStart.error.issues[0]!.path).toEqual(["quiet_hours_start_local"]);
    }
    const badEnd = policySchema.safeParse({ quiet_hours_end_local: "21:30" });
    expect(badEnd.success).toBe(false);
    if (!badEnd.success) {
      expect(badEnd.error.issues[0]!.path).toEqual(["quiet_hours_end_local"]);
    }
    const badMultiplier = policySchema.safeParse({ retry_delay_multiplier: 9 });
    expect(badMultiplier.success).toBe(false);
    if (!badMultiplier.success) {
      expect(badMultiplier.error.issues[0]!.path).toEqual(["retry_delay_multiplier"]);
    }
    const badBusy = policySchema.safeParse({ retry_max_attempts: { busy: 5 } });
    expect(badBusy.success).toBe(false);
    if (!badBusy.success) {
      expect(badBusy.error.issues[0]!.path).toEqual(["retry_max_attempts", "busy"]);
    }
  });
});
