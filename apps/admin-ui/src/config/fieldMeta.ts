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
  | "knowledge_base"
  | "stt"
  | "timing"
  | "tools"
  | "voicemail_detection"
  | "speech_advanced"
  | "policy";

export const SECTION_LABELS: Record<SectionKey, string> = {
  prompts: "Prompts",
  voice: "Voice",
  llm: "LLM",
  knowledge_base: "Knowledge Base",
  stt: "STT",
  timing: "Timing",
  tools: "Tools",
  voicemail_detection: "Voicemail",
  speech_advanced: "Speech (Advanced)",
  policy: "Policy",
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
    help: "How to open an inbound (contact-initiated) call. Supports {{variable}} tokens (insert-variable button); missing values fall back to defaults. Max 1000 chars.",
  },
  "prompts.inbound_personalization_template": {
    label: "Inbound personalization template",
    help: "Supports {{variable}} tokens (use the insert-variable button); missing values fall back to defaults. Legacy single-brace slots {contact_name} and {last_check_in_line} still work. Max 6000 chars.",
  },

  // Voice
  "voice.cartesia_voice_id": {
    label: "Voice",
    help: "Pick a voice from the curated catalog (search by name, style, or language) and use Play sample to hear it. Clear to fall back to the plugin default. A published call keeps using a deprecated voice until you switch.",
  },
  "voice.tts_model": {
    label: "TTS model",
    help: "Override the TTS model for this voice. Blank = the voice's suggested model / plugin default.",
  },
  "voice.speed": {
    label: "Speech speed",
    // Multiplier (1.0 = normal). Blank sends nothing to Cartesia, so its "normal" speed applies.
    help: "0.25–4.0 (1.0 = normal). Blank = Cartesia default (normal).",
  },
  "voice.language": { label: "Voice language", help: "Language code. Blank = default." },

  // LLM
  "llm.model": {
    label: "LLM model",
    help: "Choose a model from the curated catalog (all served via Vertex AI). A deprecated model stays selectable for published configs but is marked; pick a current model going forward.",
  },
  "llm.temperature": {
    label: "Temperature",
    // Blank passes nothing to Vertex (NOT_GIVEN), so the Gemini model's own default (1.0) applies.
    help: "0–2. Blank = model default (Gemini = 1.0).",
  },

  // STT
  "stt.model": {
    label: "STT model",
    help: "Choose a speech-to-text model from the curated catalog. A deprecated model stays selectable for published configs but is marked.",
  },
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

  // Speech advanced (all advanced). The "plugin default (N)" values are what applies when a
  // field is left blank: for VAD + endpointing + interruption the agent omits the param, so the
  // LiveKit Agents 1.5.14 / Silero VAD library default applies; turn detection is the exception —
  // blank/None is mapped agent-side to EnglishModel ("english"), not a library fall-through. See
  // services/agent/src/usan_agent/pipeline.py. Re-verify these on a livekit-agents bump.
  "speech_advanced.vad_min_silence_s": {
    label: "VAD min silence (s)",
    help: "0–5. Blank = plugin default (0.55).",
    advanced: true,
  },
  "speech_advanced.vad_activation_threshold": {
    label: "VAD activation threshold",
    help: "0–1. Blank = plugin default (0.5).",
    advanced: true,
  },
  "speech_advanced.turn_detection": {
    label: "Turn detection",
    help: "english | multilingual | vad. Blank = plugin default (english).",
    advanced: true,
  },
  "speech_advanced.min_endpointing_delay_s": {
    label: "Min endpointing delay (s)",
    help: "0–10. Must be <= max. Blank = plugin default (0.5).",
    advanced: true,
  },
  "speech_advanced.max_endpointing_delay_s": {
    label: "Max endpointing delay (s)",
    help: "0–30. Blank = plugin default (3.0).",
    advanced: true,
  },
  "speech_advanced.min_interruption_duration_s": {
    label: "Min interruption duration (s)",
    help: "0–5. Blank = plugin default (0.5).",
    advanced: true,
  },
  "speech_advanced.min_interruption_words": {
    label: "Min interruption words",
    help: "0–20. Blank = plugin default (0 = no word-count gate; the min duration above still applies).",
    advanced: true,
  },

  // Policy (per-profile quiet-hours narrowing + retry overrides — enforced
  // server-side at every consumption site; this UI only edits the config).
  "policy.quiet_hours_start_local": {
    label: "Quiet hours start (local)",
    help: "Earliest local dial time, HH:MM. May only narrow within the statutory 09:00–21:00 window. Blank = statutory 09:00.",
  },
  "policy.quiet_hours_end_local": {
    label: "Quiet hours end (local)",
    help: "Latest local dial time, HH:MM. May only narrow within the statutory 09:00–21:00 window. Blank = statutory 21:00.",
  },
  "policy.retry_delay_multiplier": {
    label: "Retry delay multiplier",
    help: "Scales every retry-ladder delay uniformly. 0.5–4.0. Blank = ×1.0.",
  },
  "policy.retry_max_attempts": {
    label: "Retry max attempts",
    help: "Per-status caps on the chain-global attempt number: a call ending with a status retries only while the chain's attempt number is at or below its cap — there are no per-status counters. 0 disables retries for that status; blank keeps the builtin. Attempts beyond the built-in ladder reuse the final rung's delay.",
  },
  "policy.retry_max_attempts.no_answer": {
    label: "No answer",
    help: "Chain-global attempt cap for no-answer outcomes. 0–4. Blank = builtin (2).",
  },
  "policy.retry_max_attempts.voicemail_left": {
    label: "Voicemail left",
    help: "Chain-global attempt cap for voicemail outcomes. 0–4. Blank = builtin (1).",
  },
  "policy.retry_max_attempts.busy": {
    label: "Busy",
    help: "Chain-global attempt cap for busy outcomes. 0–4. Blank = builtin (1).",
  },
  "policy.retry_max_attempts.failed": {
    label: "Failed",
    help: "Chain-global attempt cap for failed outcomes. 0–4. Blank = builtin (1).",
  },
};
