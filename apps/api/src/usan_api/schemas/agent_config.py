"""The admin-editable agent configuration document (design spec Appendix A).

Stored as JSONB in ``agent_profiles.draft_config`` and frozen into
``agent_profile_versions.config`` on publish. Validated here so the JSONB is
structured, not free-form. Defaults reproduce the agent's current hardcoded
constants (services/agent: pipeline.py, check_in.py, worker.py) so a new profile
behaves like today's system. ``None`` on an optional knob means "use the agent
plugin default".
"""

import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from usan_api.schemas.tool_catalog import TOOL_NAMES
from usan_api.schemas.variable_catalog import BUILTIN_NAMES, PHI_BUILTIN_NAMES

# Personalization slots allowed in the inbound template (check_in.py rendering).
# Kept for any external code that may import it; no longer used by the validators.
ALLOWED_TEMPLATE_SLOTS = frozenset({"elder_name", "last_check_in_line"})

# Phase 2 token syntax: {{ name }} with optional inner spaces (design contract D/E).
# Mirrors services/agent prompt_vars.TOKEN_RE so the two layers agree on what a token is.
_TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
# Legacy single-brace personalization slots kept for already-published configs.
_LEGACY_SLOT_RE = re.compile(r"\{(elder_name|last_check_in_line)\}")


def _reject_stray_braces_after_tokens(value: str, *, allow_legacy_slots: bool) -> str:
    """Field-tiered brace check (design §5.1).

    Strip every well-formed ``{{token}}`` (and, when ``allow_legacy_slots``, the two
    legacy single-brace slots) then reject any ``{``/``}`` that remains. Unknown
    ``{{var}}`` NAMES are intentionally NOT rejected here — they are surfaced as
    non-fatal warnings on the save response (warn-don't-block). Substitution is
    token-scoped agent-side (never str.format), so the leftover-brace check only
    guards against typos in the short one-line fields.
    """
    stripped = _TOKEN_RE.sub("", value)
    if allow_legacy_slots:
        stripped = _LEGACY_SLOT_RE.sub("", stripped)
    if "{" in stripped or "}" in stripped:
        raise ValueError("must not contain a stray '{' or '}' outside a {{token}}")
    return value


def unknown_tokens(text: str, known_names: frozenset[str] = frozenset()) -> list[str]:
    """Return the ``{{var}}`` token names in ``text`` that are not catalog built-ins.

    ``known_names`` lets a caller treat declared custom variables as known too. The
    result is de-duplicated and keeps first-seen order so the warning list reads
    deterministically. Used to populate the additive ``warnings`` field on the
    profile save/validate response (design §5.1).
    """
    seen: list[str] = []
    for name in _TOKEN_RE.findall(text):
        if name in BUILTIN_NAMES or name in known_names:
            continue
        if name not in seen:
            seen.append(name)
    return seen


class PromptsConfig(BaseModel):
    # system_prompt and checkin_flow_instructions are large free-form behavior fields
    # that the agent passes verbatim to the LLM as instructions (pipeline.py /
    # check_in.py). They allow braces and a generous cap to hold migrated prompts
    # full of {{variable}} tokens. All prompt fields (including the inbound
    # personalization template) are token-substituted via prompt_vars.substitute();
    # legacy single-brace slots ({elder_name}, {last_check_in_line}) are str.replace-d,
    # not str.format-ed.
    system_prompt: str = Field(min_length=1, max_length=24000)
    greeting: str = Field(min_length=1, max_length=1000)
    recording_disclosure: str = Field(min_length=1, max_length=1000)
    voicemail_message: str = Field(min_length=1, max_length=1000)
    checkin_flow_instructions: str = Field(min_length=1, max_length=24000)
    goodbye_message: str = Field(min_length=1, max_length=1000)
    inbound_opening: str = Field(min_length=1, max_length=1000)
    inbound_personalization_template: str = Field(min_length=1, max_length=6000)

    # Field-tiered braces (design §5.1). Short literal fields accept {{tokens}} but
    # reject a stray lone brace (a typo in a one-line string). system_prompt and
    # checkin_flow_instructions stay permissive (NOT listed here) — they carry large
    # pasted prompts full of arbitrary braces.
    @field_validator(
        "greeting",
        "recording_disclosure",
        "voicemail_message",
        "goodbye_message",
        "inbound_opening",
    )
    @classmethod
    def _tokens_only_no_stray_braces(cls, v: str) -> str:
        return _reject_stray_braces_after_tokens(v, allow_legacy_slots=False)

    # The inbound template additionally tolerates its two legacy single-brace slots
    # ({elder_name}/{last_check_in_line}) so old published snapshots still validate.
    @field_validator("inbound_personalization_template")
    @classmethod
    def _tokens_plus_legacy_slots(cls, v: str) -> str:
        return _reject_stray_braces_after_tokens(v, allow_legacy_slots=True)


# Prompt fields spoken before the caller's identity is confirmed or to voicemail.
# A PHI variable here risks disclosing health info to an unintended listener.
# Mirrors apps/admin-ui .../phiTokens.ts SENSITIVE_PROMPT_FIELDS.
SENSITIVE_PROMPT_FIELDS: tuple[str, ...] = (
    "greeting",
    "inbound_opening",
    "recording_disclosure",
    "voicemail_message",
)


def phi_tokens_in_sensitive_fields(prompts: PromptsConfig) -> list[str]:
    """Advisory warnings for PHI variables used in pre-identity / voicemail fields.

    Non-fatal (warn-don't-block). One message per distinct (field, PHI token), in
    field-then-first-seen order, so the warning list reads deterministically.
    """
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    for field in SENSITIVE_PROMPT_FIELDS:
        text: str = getattr(prompts, field)
        for name in _TOKEN_RE.findall(text):
            if name in PHI_BUILTIN_NAMES and (field, name) not in seen:
                seen.add((field, name))
                token = "{{" + name + "}}"
                warnings.append(
                    f"{token} in '{field}' may disclose protected health information "
                    f"before the caller's identity is confirmed (or to voicemail)."
                )
    return warnings


class VoiceConfig(BaseModel):
    cartesia_voice_id: str | None = Field(default=None, min_length=1, max_length=200)
    tts_model: str | None = Field(default=None, min_length=1, max_length=100)
    speed: float | None = Field(default=None, ge=0.25, le=4.0)
    language: str | None = Field(default=None, min_length=1, max_length=20)


class LLMConfig(BaseModel):
    model: str = Field(default="gemini-3.1-flash-lite", min_length=1, max_length=200)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


class STTConfig(BaseModel):
    model: str = Field(default="ink-whisper", min_length=1, max_length=200)
    language: str | None = Field(default=None, min_length=1, max_length=20)


class TimingConfig(BaseModel):
    answer_timeout_s: float = Field(default=50.0, ge=5.0, le=180.0)
    max_call_duration_s: int = Field(default=1800, ge=60, le=7200)

    @model_validator(mode="after")
    def _duration_exceeds_answer_timeout(self) -> TimingConfig:
        # The agent arms the max-duration watchdog at session start on inbound (before
        # any answer), so a cap at or below the answer-wait could fire during the
        # greeting. The per-field ranges alone allow e.g. answer=180 with max=60, so
        # enforce the cross-field relationship here.
        if self.max_call_duration_s <= self.answer_timeout_s:
            raise ValueError(
                f"max_call_duration_s ({self.max_call_duration_s}) must be greater than "
                f"answer_timeout_s ({self.answer_timeout_s})"
            )
        return self


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
    # No `sms` field in Part A (on either copy). Part D adds `sms: SmsConfig | None = None`
    # here AND on the agent copy (services/agent agent_config.ToolsConfig), together with
    # the write-side `sms` block + SmsConfig (with the PHI hard-block). The agent's
    # _select_tools reads `sms` defensively via getattr so it stays safe until Part D
    # lands; configs published without an `sms` block deserialize cleanly on both sides.

    @field_validator("enabled")
    @classmethod
    def _known_tools(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if t not in TOOL_NAMES]
        if bad:
            raise ValueError(f"unknown tool(s): {', '.join(sorted(set(bad)))}")
        return v


class VoicemailDetectionConfig(BaseModel):
    window_s: float = Field(default=3.0, ge=0.5, le=30.0)
    # Empty list means "use the agent's built-in detection patterns".
    trigger_phrases: list[str] = Field(default_factory=list)


class SpeechAdvancedConfig(BaseModel):
    # None on each → use the LiveKit plugin default (silero VAD / EnglishModel etc).
    vad_min_silence_s: float | None = Field(default=None, ge=0.0, le=5.0)
    vad_activation_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    turn_detection: Literal["english", "multilingual", "vad"] | None = None
    min_endpointing_delay_s: float | None = Field(default=None, ge=0.0, le=10.0)
    max_endpointing_delay_s: float | None = Field(default=None, ge=0.0, le=30.0)
    min_interruption_duration_s: float | None = Field(default=None, ge=0.0, le=5.0)
    min_interruption_words: int | None = Field(default=None, ge=0, le=20)

    @model_validator(mode="after")
    def _endpointing_order(self) -> SpeechAdvancedConfig:
        mn = self.min_endpointing_delay_s
        mx = self.max_endpointing_delay_s
        if mn is not None and mx is not None and mn > mx:
            raise ValueError(
                f"min_endpointing_delay_s ({mn}) must be <= max_endpointing_delay_s ({mx})"
            )
        return self


# FORWARD-COMPATIBILITY INVARIANT: version snapshots in agent_profile_versions.config
# are immutable and long-lived, and are re-validated through AgentConfig on every read
# (ProfileDetail/VersionDetail.from_model). Any NEW field added here MUST be Optional
# with a default (and any tightened constraint must stay satisfiable by older configs),
# or previously-published rows will fail validation and 500 on read. See
# test_agent_config_schema.test_legacy_config_still_deserializes.
class AgentConfig(BaseModel):
    # Mirrors the frozen agent-side copy (services/agent/.../agent_config.py): a
    # resolved/default config is read-only after construction. apps/api only
    # .model_dump()s and validates these, never field-assigns, so freezing is safe
    # and keeps the two copies' intent in sync.
    model_config = ConfigDict(frozen=True)

    prompts: PromptsConfig
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    timing: TimingConfig = Field(default_factory=TimingConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    voicemail_detection: VoicemailDetectionConfig = Field(default_factory=VoicemailDetectionConfig)
    speech_advanced: SpeechAdvancedConfig = Field(default_factory=SpeechAdvancedConfig)


# Defaults below are copied verbatim from the agent's current constants so a new
# profile reproduces today's behavior. Keep in sync if those constants change.
DEFAULT_AGENT_CONFIG = AgentConfig(
    prompts=PromptsConfig(
        system_prompt=(
            "You are a warm, patient daily check-in assistant from USAN Retirement.\n"
            "You are speaking to an elder over the phone. Speak slowly, clearly, and kindly.\n"
            "Keep responses short — one or two sentences. Pause to let them respond.\n"
        ),
        greeting=("Hello! This is your daily check-in from USAN. How are you feeling today?"),
        recording_disclosure=(
            "Before we begin, please know that this call is recorded for quality and "
            "to support your care."
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


class ResolvedAgentConfig(BaseModel):
    """The published config resolved for a call/direction, plus provenance.

    ``source`` is "resolved" when a published profile matched the precedence walk,
    or "default" when nothing resolved and the server's DEFAULT_AGENT_CONFIG is
    returned. ``profile_id``/``version`` are the live snapshot's identity (non-PHI),
    useful for agent-side logging and debugging.
    """

    source: Literal["resolved", "default"]
    profile_id: uuid.UUID | None = None
    version: int | None = None
    config: AgentConfig
