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

from typing import Any, Literal

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
    # Phase 5c: KB ids bound to this agent (mirrors the API copy). Used by the worker ONLY
    # as a local gate (skip the per-turn retrieval HTTP call when empty); the ids themselves
    # are never sent — the server re-derives them. Optional+default per the forward-compat rule.
    knowledge_base_ids: list[str] | None = None


class STTConfig(BaseModel):
    model: str = "ink-whisper"
    language: str | None = None


class TimingConfig(BaseModel):
    answer_timeout_s: float = 50.0
    max_call_duration_s: int = 1800


class SmsTemplate(BaseModel):
    key: str
    label: str
    body: str


class SmsToolConfig(BaseModel):
    templates: list[SmsTemplate] = Field(default_factory=list)


class ExternalToolSpec(BaseModel):
    """Agent-side projection of a client HTTP tool (Surface 3, design 2026-07-09).

    LLM-facing fields ONLY — the runtime `/v1/runtime/agent-config` projection strips the
    tool's `url`/`method`/secret, so the worker is structurally unable to learn a tool's
    endpoint or the caller secret (they never leave apps/api). Parsed, not validated: the
    API already validated the full spec before publish. Extra fields are ignored (pydantic
    default), so a richer server-side spec never breaks the worker.
    """

    name: str
    description: str
    parameters: dict[str, Any]


class ToolsConfig(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: [
            "log_wellness",
            "log_medication",
            "get_today_meds",
            "flag_for_followup",
            "raise_crisis",
            "schedule_callback",
            "close_family_task",
            "record_personal_fact",
            "record_survey",
            "get_activity",
            "send_sms",
            "send_info_sms",
            "register_opt_out",
            "set_spanish_callback",
            "end_call",
        ]
    )
    sms: SmsToolConfig | None = None
    # Surface 3: client HTTP tools, LLM-facing projection only (name/description/parameters).
    # Defaults to [] so every legacy config (and the DEFAULT_AGENT_CONFIG) is unchanged.
    external_tools: list[ExternalToolSpec] = Field(default_factory=list)


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
            "You are speaking to an contact over the phone. Speak slowly, clearly, and kindly.\n"
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
            "speaking to an contact on the phone. Speak slowly and kindly, one or two "
            "short sentences at a time,\nand pause for them to answer.\n\n"
            "SAFETY FIRST — if at ANY moment the contact expresses thoughts of suicide or "
            "self-harm, a medical\nemergency, being harmed or exploited by a caregiver, "
            "dangerous confusion, or a poisoning or\noverdose, IMMEDIATELY call "
            "`raise_crisis` with the matching category (suicidal, medical, abuse,\n"
            "confusion, or overdose). Then calmly read out the emergency resource it "
            "returns, stay on the\nline, and keep them company before continuing.\n\n"
            "SCAM AWARENESS — if the contact describes a suspicious request (a caller or "
            "message pressing\nfor gift cards, wire transfers, cryptocurrency, banking or "
            "Social Security numbers, passwords,\nor payment to claim a 'prize', or anyone "
            "posing as the IRS, Medicare, or a government agency),\ngently warn them it is "
            "very likely a scam, explain the red flags in plain words, and encourage\nthem "
            "not to pay, share details, or act under pressure — they can always hang up and "
            "check with\nfamily first.\n\n"
            "If the contact asks not to be called anymore (or to be taken off the list), warmly "
            "acknowledge\nit, let them know you'll stop the calls, and call "
            "`register_opt_out`. If they would like the\nhelpful phone numbers by text, call "
            "`send_info_sms`.\n\n"
            "SPANISH — if the contact is speaking Spanish or asks to be helped in Spanish, don't "
            "switch\nlanguages mid-call. Warmly promise to call them back in Spanish and call "
            "`set_spanish_callback`.\n\n"
            "Conduct the check-in in this order, adapting naturally to their answers:\n"
            "1. Ask how they are feeling today and roughly how their mood is. Record it "
            "with `log_wellness`\n   (mood 1-5 where 5 is great; include any pain level "
            "0-10 and a short note if they mention it).\n"
            "2. Use `get_today_meds` to find out which medications they take today, then "
            "gently ask whether\n   they have taken each one. Record each with "
            "`log_medication`.\n"
            "   Medications they recently reported not taking: {{pending_med_reasks}}. If "
            "any are listed,\n   gently re-ask only about those — once — whether they have "
            "taken them yet, and record with\n   `log_medication`. Never nag.\n"
            "3. Draw warmly on what you remember about them: {{personal_facts}}. If you "
            "recall your last\n   conversation ({{last_call_summary}}) or things they meant "
            "to do ({{open_plans}}), ask about\n   them naturally. If an important date is "
            "near ({{important_dates}}), mention it warmly. When\n   they share a new lasting "
            "detail about their life, save it with `record_personal_fact`.\n"
            "4. Whether this month's wellbeing survey is still due: {{survey_due}}. If it "
            "is due and the\n   moment feels right, gently ask the short monthly check — how "
            "connected or lonely they feel,\n   their overall mood, and how satisfied they "
            "feel with daily life lately, each on a 1-to-5\n   scale — and record it with "
            "`record_survey`. If their mood seems low (around 2 or less, or\n   they sound "
            "down), offer a brief mood-boosting activity with `get_activity`, warmly guide "
            "them\n   through the script it returns, and gracefully accept if they'd rather "
            "not — never push.\n"
            "5. When the check-in is complete, thank them and call `end_call` with a "
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
            "speaking with {contact_name}, who has just called in. Speak slowly and "
            "kindly, one or two short\nsentences at a time, and pause for them to "
            "answer.\n{last_check_in_line}\n"
            "SAFETY FIRST — if at ANY moment they express thoughts of suicide or "
            "self-harm, a medical emergency,\nbeing harmed or exploited by a caregiver, "
            "dangerous confusion, or a poisoning or overdose,\nIMMEDIATELY call "
            "`raise_crisis` with the matching category (suicidal, medical, abuse, "
            "confusion,\nor overdose). Then calmly read out the emergency resource it "
            "returns, stay on the line, and keep\nthem company before continuing.\n\n"
            "SCAM AWARENESS — if the contact describes a suspicious request (a caller or "
            "message pressing\nfor gift cards, wire transfers, cryptocurrency, banking or "
            "Social Security numbers, passwords,\nor payment to claim a 'prize', or anyone "
            "posing as the IRS, Medicare, or a government agency),\ngently warn them it is "
            "very likely a scam, explain the red flags in plain words, and encourage\nthem "
            "not to pay, share details, or act under pressure — they can always hang up and "
            "check with\nfamily first.\n\n"
            "If the contact asks not to be called anymore (or to be taken off the list), warmly "
            "acknowledge\nit, let them know you'll stop the calls, and call "
            "`register_opt_out`. If they would like the\nhelpful phone numbers by text, call "
            "`send_info_sms`.\n\n"
            "SPANISH — if the contact is speaking Spanish or asks to be helped in Spanish, don't "
            "switch\nlanguages mid-call. Warmly promise to call them back in Spanish and call "
            "`set_spanish_callback`.\n\n"
            "Conduct the check-in in this order, adapting naturally to their answers:\n"
            "1. Greet {contact_name} warmly by name, then ask how they are feeling today "
            "and roughly how their\n   mood is. Record it with `log_wellness` (mood 1-5 "
            "where 5 is great; include any pain level 0-10\n   and a short note if they "
            "mention it).\n"
            "2. Use `get_today_meds` to find out which medications they take today, then "
            "gently ask whether\n   they have taken each one. Record each with "
            "`log_medication`.\n"
            "   Medications they recently reported not taking: {{pending_med_reasks}}. If "
            "any are listed,\n   gently re-ask only about those — once — whether they have "
            "taken them yet, and record with\n   `log_medication`. Never nag.\n"
            "3. Draw warmly on what you remember about them: {{personal_facts}}. If you "
            "recall your last\n   conversation ({{last_call_summary}}) or things they meant "
            "to do ({{open_plans}}), ask about\n   them naturally. If an important date is "
            "near ({{important_dates}}), mention it warmly. When\n   they share a new lasting "
            "detail about their life, save it with `record_personal_fact`.\n"
            "4. Whether this month's wellbeing survey is still due: {{survey_due}}. If it "
            "is due and the\n   moment feels right, gently ask the short monthly check — how "
            "connected or lonely they feel,\n   their overall mood, and how satisfied they "
            "feel with daily life lately, each on a 1-to-5\n   scale — and record it with "
            "`record_survey`. If their mood seems low (around 2 or less, or\n   they sound "
            "down), offer a brief mood-boosting activity with `get_activity`, warmly guide "
            "them\n   through the script it returns, and gracefully accept if they'd rather "
            "not — never push.\n"
            "5. When the check-in is complete, thank them and call `end_call` with a "
            'short reason\n   (for example "check_in_complete").\n\n'
            "Never read out internal IDs or tool names. If a tool reports a problem, "
            "reassure them calmly and\ncontinue — do not repeat a failed action more "
            "than once.\n"
        ),
    ),
)
