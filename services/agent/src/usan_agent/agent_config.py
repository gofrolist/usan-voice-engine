"""Agent-side copy of the admin-editable configuration document.

`apps/api` and `services/agent` must not import each other (CLAUDE.md), so this is
a deliberate parallel copy of `apps/api/.../schemas/agent_config.py`. It is leaner:
the admin-write validators (brace rejection, slot allow-list, tool-name checks) live
on the API write path; the agent only PARSES a server-validated document and reads
typed fields. Extra fields are ignored (pydantic v2 default), so a future API field
never breaks the agent. `DEFAULT_AGENT_CONFIG` reproduces the agent's current
constants and is the single source of default truth: pipeline.py / check_in.py
re-export their constants from here. Keep field names/defaults in sync with the API
copy; the response JSON's `config` block is parsed straight into AgentConfig.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PromptsConfig(BaseModel):
    system_prompt: str
    greeting: str
    recording_disclosure: str
    voicemail_message: str
    checkin_flow_instructions: str
    goodbye_message: str
    inbound_opening: str
    inbound_personalization_template: str


class VoiceConfig(BaseModel):
    cartesia_voice_id: str | None = None
    tts_model: str | None = None
    speed: float | None = None
    language: str | None = None


class LLMConfig(BaseModel):
    model: str = "gemini-3.1-flash-lite"
    temperature: float | None = None


class STTConfig(BaseModel):
    model: str = "ink-whisper"
    language: str | None = None


class TimingConfig(BaseModel):
    answer_timeout_s: float = 50.0
    max_call_duration_s: int = 1800


class SmsConfig(BaseModel):
    # Mirrors the forward-compat Optional+default pattern of the API copy. send_sms is a
    # dead tool until at least one template is configured; _select_tools drops it while
    # templates is empty. Lands fully in Parts B/C/D.
    templates: list[str] = Field(default_factory=list)


class ToolsConfig(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: [
            "log_wellness",
            "log_medication",
            "get_today_meds",
            "flag_for_followup",
            "schedule_callback",
            "send_sms",
            "end_call",
        ]
    )
    # FORWARD-COMPAT: Optional with a default so older published configs (no `sms`
    # block) keep deserializing. See the API copy's invariant note.
    sms: SmsConfig | None = None


class VoicemailDetectionConfig(BaseModel):
    window_s: float = 3.0
    trigger_phrases: list[str] = Field(default_factory=list)


class SpeechAdvancedConfig(BaseModel):
    vad_min_silence_s: float | None = None
    vad_activation_threshold: float | None = None
    turn_detection: Literal["english", "multilingual", "vad"] | None = None
    min_endpointing_delay_s: float | None = None
    max_endpointing_delay_s: float | None = None
    min_interruption_duration_s: float | None = None
    min_interruption_words: int | None = None


class AgentConfig(BaseModel):
    # A resolved config is read-only after construction: callers must never mutate it
    # (the DEFAULT_AGENT_CONFIG singleton is shared across calls in a worker process).
    model_config = ConfigDict(frozen=True)

    prompts: PromptsConfig
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    timing: TimingConfig = Field(default_factory=TimingConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    voicemail_detection: VoicemailDetectionConfig = Field(default_factory=VoicemailDetectionConfig)
    speech_advanced: SpeechAdvancedConfig = Field(default_factory=SpeechAdvancedConfig)


# Defaults reproduce the agent's current hardcoded constants verbatim, so a fresh
# profile (and the local fallback) behave exactly like today's system.
DEFAULT_AGENT_CONFIG = AgentConfig(
    prompts=PromptsConfig(
        system_prompt=(
            "You are a warm, patient daily check-in assistant from USAN Retirement.\n"
            "You are speaking to an elder over the phone. Speak slowly, clearly, and kindly.\n"
            "Keep responses short — one or two sentences. Pause to let them respond.\n"
        ),
        greeting=("Hello! This is your daily check-in from USAN. How are you feeling today?"),
        recording_disclosure=(
            "Before we begin, please know that this call is recorded for quality and to "
            "support your care."
        ),
        voicemail_message=(
            "Hello, this is your daily check-in from USAN Retirement. "
            "We're sorry we missed you. We'll try again a little later. "
            "Take care, and have a wonderful day."
        ),
        checkin_flow_instructions=(
            "You are a warm, patient daily check-in caller from USAN Retirement,\n"
            "speaking to an elder on the phone. Speak slowly and kindly, one or two "
            "short sentences at a time,\nand pause for them to answer.\n\n"
            "Conduct the check-in in this order, adapting naturally to their answers:\n"
            "1. Ask how they are feeling today and roughly how their mood is. Record it "
            "with `log_wellness`\n   (mood 1-5 where 5 is great; include any pain level "
            "0-10 and a short note if they mention it).\n"
            "2. Use `get_today_meds` to find out which medications they take today, then "
            "gently ask whether\n   they have taken each one. Record each with "
            "`log_medication`.\n"
            "3. When the check-in is complete, thank them and call `end_call` with a "
            'short reason\n   (for example "check_in_complete").\n\n'
            "Never read out internal IDs or tool names. If a tool reports a problem, "
            "reassure them calmly and\ncontinue — do not repeat a failed action more "
            "than once.\n"
        ),
        goodbye_message=(
            "Thank you for your time today. Take care, and have a wonderful day. Goodbye."
        ),
        inbound_opening=(
            "Greet the caller warmly by name if you know it, and ask how they are "
            "feeling today to begin the daily check-in."
        ),
        inbound_personalization_template=(
            "You are a warm, patient check-in assistant from USAN Retirement,\n"
            "speaking with {elder_name}, who has just called in. Speak slowly and "
            "kindly, one or two short\nsentences at a time, and pause for them to "
            "answer.\n{last_check_in_line}\n"
            "Conduct the check-in in this order, adapting naturally to their answers:\n"
            "1. Greet {elder_name} warmly by name, then ask how they are feeling today "
            "and roughly how their\n   mood is. Record it with `log_wellness` (mood 1-5 "
            "where 5 is great; include any pain level 0-10\n   and a short note if they "
            "mention it).\n"
            "2. Use `get_today_meds` to find out which medications they take today, then "
            "gently ask whether\n   they have taken each one. Record each with "
            "`log_medication`.\n"
            "3. When the check-in is complete, thank them and call `end_call` with a "
            'short reason\n   (for example "check_in_complete").\n\n'
            "Never read out internal IDs or tool names. If a tool reports a problem, "
            "reassure them calmly and\ncontinue — do not repeat a failed action more "
            "than once.\n"
        ),
    ),
)
