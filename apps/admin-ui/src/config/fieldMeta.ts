// Per-field metadata driving the editor sections: label, help text, and whether a
// field belongs in the collapsed "Advanced" panel. Keys are dotted paths into
// AgentConfig so DiffView / sections can resolve them uniformly.

export interface FieldMeta {
  label: string;
  help: string;
  advanced?: boolean;
}

export type SectionKey =
  | "prompts"
  | "voice"
  | "llm"
  | "stt"
  | "timing"
  | "tools"
  | "voicemail_detection"
  | "speech_advanced";

export const SECTION_LABELS: Record<SectionKey, string> = {
  prompts: "Prompts",
  voice: "Voice",
  llm: "LLM",
  stt: "STT",
  timing: "Timing",
  tools: "Tools",
  voicemail_detection: "Voicemail",
  speech_advanced: "Speech (Advanced)",
};

export const fieldMeta: Record<string, FieldMeta> = {
  // Prompts
  "prompts.system_prompt": {
    label: "System prompt",
    help: "Base persona/instructions. Supports {{variable}} tokens (use the insert-variable button); a missing value falls back to the variable's default. Up to 24,000 chars.",
  },
  "prompts.greeting": {
    label: "Greeting",
    help: "First thing said on an outbound call. Supports {{variable}} tokens (use the insert-variable button); a missing value falls back to its default. Max 1000 chars.",
  },
  "prompts.recording_disclosure": {
    label: "Recording disclosure",
    help: "Recording notice read at call start. Supports {{variable}} tokens (insert-variable button); missing values fall back to defaults. Max 1000 chars.",
  },
  "prompts.voicemail_message": {
    label: "Voicemail message",
    help: "Left when a voicemail is detected. Supports {{variable}} tokens (insert-variable button); missing values fall back to defaults. Max 1000 chars.",
  },
  "prompts.checkin_flow_instructions": {
    label: "Check-in flow instructions",
    help: "Step-by-step check-in script. Supports {{variable}} tokens (use the insert-variable button); a missing value falls back to the variable's default. Up to 24,000 chars.",
  },
  "prompts.goodbye_message": {
    label: "Goodbye message",
    help: "Said before hangup. Supports {{variable}} tokens (insert-variable button); missing values fall back to defaults. Max 1000 chars.",
  },
  "prompts.inbound_opening": {
    label: "Inbound opening",
    help: "How to open an inbound (elder-initiated) call. Supports {{variable}} tokens (insert-variable button); missing values fall back to defaults. Max 1000 chars.",
  },
  "prompts.inbound_personalization_template": {
    label: "Inbound personalization template",
    help: "Supports {{variable}} tokens (use the insert-variable button); missing values fall back to defaults. Legacy single-brace slots {elder_name} and {last_check_in_line} still work. Max 6000 chars.",
  },

  // Voice
  "voice.cartesia_voice_id": {
    label: "Cartesia voice ID",
    help: "TTS voice id. Leave blank for the plugin default.",
  },
  "voice.tts_model": { label: "TTS model", help: "TTS model name. Blank = default." },
  "voice.speed": { label: "Speech speed", help: "0.25–4.0. Blank = default." },
  "voice.language": { label: "Voice language", help: "Language code. Blank = default." },

  // LLM
  "llm.model": { label: "LLM model", help: "Model name." },
  "llm.temperature": { label: "Temperature", help: "0–2. Blank = plugin default." },

  // STT
  "stt.model": { label: "STT model", help: "Speech-to-text model name." },
  "stt.language": { label: "STT language", help: "Language code. Blank = default." },

  // Timing
  "timing.answer_timeout_s": {
    label: "Answer timeout (s)",
    help: "Ring time before giving up. 5–180.",
  },
  "timing.max_call_duration_s": {
    label: "Max call duration (s)",
    help: "Hard cap on call length. 60–7200.",
  },

  // Tools
  "tools.enabled": {
    label: "Enabled tools",
    help: "Which tools the agent may call this profile. The available tools come from the server catalog; end_call is always on.",
  },
  "tools.sms": {
    label: "SMS templates",
    help: "Operator-authored text templates the agent can send by key (it never writes free text). Bodies may use non-PHI variables only — a PHI variable is rejected (SMS is unencrypted). send_sms is offered to the agent only when at least one template exists.",
  },

  // Voicemail detection
  "voicemail_detection.window_s": {
    label: "Detection window (s)",
    help: "Listen window for voicemail detection. 0.5–30.",
  },
  "voicemail_detection.trigger_phrases": {
    label: "Trigger phrases",
    help: "Extra phrases that signal a voicemail. Empty = built-in patterns.",
  },

  // Speech advanced (all advanced)
  "speech_advanced.vad_min_silence_s": {
    label: "VAD min silence (s)",
    help: "0–5. Blank = plugin default.",
    advanced: true,
  },
  "speech_advanced.vad_activation_threshold": {
    label: "VAD activation threshold",
    help: "0–1. Blank = plugin default.",
    advanced: true,
  },
  "speech_advanced.turn_detection": {
    label: "Turn detection",
    help: "english | multilingual | vad. Blank = plugin default.",
    advanced: true,
  },
  "speech_advanced.min_endpointing_delay_s": {
    label: "Min endpointing delay (s)",
    help: "0–10. Must be <= max. Blank = plugin default.",
    advanced: true,
  },
  "speech_advanced.max_endpointing_delay_s": {
    label: "Max endpointing delay (s)",
    help: "0–30. Blank = plugin default.",
    advanced: true,
  },
  "speech_advanced.min_interruption_duration_s": {
    label: "Min interruption duration (s)",
    help: "0–5. Blank = plugin default.",
    advanced: true,
  },
  "speech_advanced.min_interruption_words": {
    label: "Min interruption words",
    help: "0–20. Blank = plugin default.",
    advanced: true,
  },
};
