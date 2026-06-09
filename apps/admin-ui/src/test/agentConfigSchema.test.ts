import { describe, expect, it } from "vitest";
import { agentConfigSchema, type AgentConfigForm } from "../config/agentConfigSchema";

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
        "Speaking with {elder_name}. {last_check_in_line} Begin the check-in.",
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
    tools: { enabled: ["log_wellness", "log_medication", "get_today_meds", "end_call"] },
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

  it("rejects a brace in the greeting", () => {
    const cfg = validConfig();
    cfg.prompts.greeting = "Hello {name}, how are you?";
    expect(agentConfigSchema.safeParse(cfg).success).toBe(false);
  });

  it("rejects an unknown template slot", () => {
    const cfg = validConfig();
    cfg.prompts.inbound_personalization_template = "Hello {first_name}, welcome.";
    expect(agentConfigSchema.safeParse(cfg).success).toBe(false);
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
});
