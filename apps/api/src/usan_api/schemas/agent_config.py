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
from datetime import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from usan_api.schemas.model_catalog import LLM_MODEL_NAMES, STT_MODEL_NAMES
from usan_api.schemas.tool_catalog import TOOL_NAMES
from usan_api.schemas.variable_catalog import BUILTIN_NAMES, PHI_BUILTIN_NAMES
from usan_api.schemas.voice_catalog import VOICE_IDS

# Personalization slots resolved by the agent's token substitution (check_in.py).
# Documented for the editor's slot hints; substitution still resolves these single-brace
# slots, so the set stays valid even though no field-validator references it anymore.
ALLOWED_TEMPLATE_SLOTS = frozenset({"contact_name", "last_check_in_line"})

# Phase 2 token syntax: {{ name }} with optional inner spaces (design contract D/E).
# Mirrors services/agent prompt_vars.TOKEN_RE so the two layers agree on what a token is.
_TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def _reject_stray_braces_after_tokens(value: str) -> str:
    """Stray-brace guard for the SHORT one-line prompt fields (design §5.1).

    Strip every well-formed ``{{token}}`` then reject any ``{``/``}`` that remains — a
    lone brace in a short user-facing line (greeting, voicemail, ...) is almost always a
    typo. Unknown ``{{var}}`` NAMES are intentionally NOT rejected — they surface as
    non-fatal warnings on the save response (warn-don't-block). Substitution is
    token-scoped agent-side (never str.format), so a leftover brace is otherwise inert;
    the large free-form fields (system_prompt, checkin_flow_instructions,
    inbound_personalization_template) skip this check entirely.
    """
    stripped = _TOKEN_RE.sub("", value)
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
    # system_prompt, checkin_flow_instructions and inbound_personalization_template are
    # large free-form prompt fields the agent passes to the LLM (pipeline.py /
    # check_in.py). They allow arbitrary braces and a generous cap to hold migrated
    # prompts full of {{variable}} tokens. All prompt fields are token-substituted via
    # prompt_vars.substitute() (legacy single-brace slots {contact_name}/
    # {last_check_in_line} are str.replace-d, never str.format-ed), so a stray brace is
    # inert at substitution time.
    system_prompt: str = Field(min_length=1, max_length=24000)
    greeting: str = Field(min_length=1, max_length=1000)
    recording_disclosure: str = Field(min_length=1, max_length=1000)
    voicemail_message: str = Field(min_length=1, max_length=1000)
    checkin_flow_instructions: str = Field(min_length=1, max_length=24000)
    goodbye_message: str = Field(min_length=1, max_length=1000)
    inbound_opening: str = Field(min_length=1, max_length=1000)
    inbound_personalization_template: str = Field(min_length=1, max_length=6000)

    # Field-tiered braces (design §5.1). Only the SHORT one-line fields reject a stray
    # lone brace (a likely typo); they still accept {{tokens}}. The large free-form
    # fields — system_prompt, checkin_flow_instructions and
    # inbound_personalization_template — are permissive (NOT listed here): they hold
    # multi-paragraph pasted prompts with arbitrary braces, and a stray-brace check on
    # READ would make a stored config with any lone brace fail to deserialize (500 on
    # GET). Substitution is token-scoped, so a lone brace is harmless.
    @field_validator(
        "greeting",
        "recording_disclosure",
        "voicemail_message",
        "goodbye_message",
        "inbound_opening",
    )
    @classmethod
    def _tokens_only_no_stray_braces(cls, v: str) -> str:
        return _reject_stray_braces_after_tokens(v)


# Prompt fields spoken before the caller's identity is confirmed or to voicemail.
# A PHI variable here risks disclosing health info to an unintended listener.
# Mirrors apps/admin-ui .../phiTokens.ts SENSITIVE_PROMPT_FIELDS.
SENSITIVE_PROMPT_FIELDS: tuple[str, ...] = (
    "greeting",
    "inbound_opening",
    "recording_disclosure",
    "voicemail_message",
)


def phi_tokens_in_sensitive_fields(
    prompts: PromptsConfig, *, phi_names: frozenset[str] = PHI_BUILTIN_NAMES
) -> list[str]:
    """Advisory warnings for PHI variables used in pre-identity / voicemail fields.

    Non-fatal (warn-don't-block). One message per distinct (field, PHI token), in
    field-then-first-seen order, so the warning list reads deterministically.

    ``phi_names`` lets the save path extend the check to declared custom
    variables flagged phi=true (builtins ∪ custom PHI names, spec §3.2); the
    keyword default keeps every existing caller builtin-only, zero-diff.
    """
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()
    for field in SENSITIVE_PROMPT_FIELDS:
        text: str = getattr(prompts, field)
        for name in _TOKEN_RE.findall(text):
            if name in phi_names and (field, name) not in seen:
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
    # Phase 5b: KB ids bound to this response engine for text-RAG. Encoded public ids
    # (knowledge_base_<hex>). Optional+default to satisfy the frozen/re-validate forward-compat
    # invariant. Echoed via compat_extras; consumed at chat generation.
    knowledge_base_ids: list[str] | None = Field(default=None)


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


# --- send_sms templates (Phase 3 §6.1) -------------------------------------
# Operator-authored SMS bodies the LLM selects by key (never free text). A body
# may reference ONLY non-PHI catalog variables: a PHI token (PHI_BUILTIN_NAMES)
# hard-blocks save (HTTP 422), stricter than the greeting warn-only rule, because
# SMS leaves our system unencrypted and carrier-visible (design §6.2). Token
# detection reuses the Phase 2 _TOKEN_RE so the two layers agree on what a token is.
class SmsTemplate(BaseModel):
    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=480)


def _phi_tokens_in_body(body: str) -> list[str]:
    """PHI catalog tokens used in an SMS body, de-duplicated in first-seen order."""
    seen: list[str] = []
    for name in _TOKEN_RE.findall(body):
        if name in PHI_BUILTIN_NAMES and name not in seen:
            seen.append(name)
    return seen


def _reject_phi_in_templates(templates: list[SmsTemplate]) -> None:
    for tmpl in templates:
        phi = _phi_tokens_in_body(tmpl.body)
        if phi:
            joined = ", ".join("{{" + n + "}}" for n in phi)
            raise ValueError(
                f"SMS template '{tmpl.key}' body references protected health information "
                f"({joined}); SMS bodies may use non-PHI variables only"
            )


def custom_phi_sms_violations(
    config: dict[str, Any], phi_names: frozenset[str]
) -> list[dict[str, Any]]:
    """PRIMARY enforcement for custom PHI variables in SMS bodies (spec §3.2.1).

    The pydantic validators above keep hard-blocking the 5 builtin PHI names
    unchanged — but they cannot see the ``custom_variables`` table, so the
    admin_profiles save/publish/rollback handlers (which have DB access) run
    this helper and raise a non-empty result as ``HTTPException(422,
    detail=violations)``. The client shows only a non-blocking notice for
    customs (spec §6.3), so this server 422 is the authoritative gate and the
    fabricated field-level ``loc`` —
    ``["body", "config", "tools", "sms", "templates", <i>, "body"]`` — is
    load-bearing: the client's ``tryParseFieldErrors`` parses it exactly like a
    pydantic 422 and lands the error on the offending body input.

    Walks ``config["tools"]["sms"]["templates"]`` tolerantly (absent keys /
    ``None`` mean "no SMS templates" → no violations). The message carries
    variable NAMES and field paths only — never per-call values (spec §7).
    """
    templates = ((config.get("tools") or {}).get("sms") or {}).get("templates") or []
    violations: list[dict[str, Any]] = []
    for i, tmpl in enumerate(templates):
        body = tmpl.get("body") or ""
        phi: list[str] = []
        for name in _TOKEN_RE.findall(body):
            if name in phi_names and name not in phi:
                phi.append(name)
        if phi:
            joined = ", ".join("{{" + n + "}}" for n in phi)
            violations.append(
                {
                    "loc": ["body", "config", "tools", "sms", "templates", i, "body"],
                    "msg": (
                        f"SMS template '{tmpl.get('key', '')}' body references protected "
                        f"health information ({joined}); SMS bodies may use non-PHI "
                        f"variables only"
                    ),
                    "type": "value_error.custom_phi_sms",
                }
            )
    return violations


# --- voice/model catalog membership (US2 / FR-014, research R1+R2) -----------
# HANDLER-LAYER validation mirroring custom_phi_sms_violations above. The frozen
# AgentConfig sub-models (VoiceConfig/LLMConfig/STTConfig) keep their fields as plain
# str (NEVER a Literal/enum) so a published agent_profile_versions snapshot referencing
# a withdrawn voice/model id STILL deserializes on read (FORWARD-COMPATIBILITY
# INVARIANT). Catalog membership is therefore enforced only at SAVE time, in the
# admin_profiles update_draft/publish/rollback handlers, which raise a non-empty result
# as HTTPException(422, detail=violations). The fabricated field-level loc is
# load-bearing: the client's tryParseFieldErrors parses it exactly like a pydantic 422
# and lands the error on the offending control. Messages carry the rejected id and
# field path only — never per-call values (spec §7).


def voice_violations(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Reject a voice id outside VOICE_CATALOG (FR-014). None/absent → no violation.

    ``None`` (the AgentConfig default) means "use the agent plugin default" and is
    always allowed; only a non-null id that is not in the curated catalog blocks save.
    """
    voice_id = (config.get("voice") or {}).get("cartesia_voice_id")
    if voice_id is None or voice_id in VOICE_IDS:
        return []
    return [
        {
            "loc": ["body", "config", "voice", "cartesia_voice_id"],
            "msg": (
                f"voice '{voice_id}' is not in the curated voice catalog; "
                "pick a voice from the catalog"
            ),
            "type": "value_error.unknown_voice",
        }
    ]


def model_catalog_violations(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Reject an LLM/STT model id outside the curated catalog (FR-014).

    Both fields are required str on the sub-models (a non-empty default), so each is
    always present; an id outside its kind's membership set blocks save with a
    field-level loc (``[...,"llm","model"]`` / ``[...,"stt","model"]``).
    """
    violations: list[dict[str, Any]] = []
    llm_model = (config.get("llm") or {}).get("model")
    if llm_model is not None and llm_model not in LLM_MODEL_NAMES:
        violations.append(
            {
                "loc": ["body", "config", "llm", "model"],
                "msg": (
                    f"LLM model '{llm_model}' is not in the curated model catalog; "
                    "pick a model from the catalog"
                ),
                "type": "value_error.unknown_model",
            }
        )
    stt_model = (config.get("stt") or {}).get("model")
    if stt_model is not None and stt_model not in STT_MODEL_NAMES:
        violations.append(
            {
                "loc": ["body", "config", "stt", "model"],
                "msg": (
                    f"STT model '{stt_model}' is not in the curated model catalog; "
                    "pick a model from the catalog"
                ),
                "type": "value_error.unknown_model",
            }
        )
    return violations


def catalog_violations(config: dict[str, Any]) -> list[dict[str, Any]]:
    """All voice + model catalog-membership violations for a config (save-time gate)."""
    return voice_violations(config) + model_catalog_violations(config)


class SmsToolConfig(BaseModel):
    templates: list[SmsTemplate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_phi_in_bodies(self) -> SmsToolConfig:
        _reject_phi_in_templates(self.templates)
        return self


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

    @field_validator("enabled")
    @classmethod
    def _known_tools(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if t not in TOOL_NAMES]
        if bad:
            raise ValueError(f"unknown tool(s): {', '.join(sorted(set(bad)))}")
        return v

    @model_validator(mode="after")
    def _sms_templates_no_phi(self) -> ToolsConfig:
        # HARD BLOCK (design §6.2): a PHI token in any SMS body fails to save (422).
        if self.sms is not None:
            _reject_phi_in_templates(self.sms.templates)
        return self


def sms_renders_empty_warnings(tools: ToolsConfig | None) -> list[str]:
    """Warn that non-builtin tokens in SMS bodies will render as empty text.

    ``render_sms_body``'s substitution map is builtins-minus-PHI + clock vars
    ONLY — ``dynamic_vars`` (the one channel carrying custom values) never
    enters it, so every custom token in an SMS body renders ``""`` (spec
    §3.2.1). Declared customs and undeclared tokens warn alike (hard-blocking
    only declared names would be perverse: declare → blocked, leave undeclared
    → allowed). Warn-don't-block: phi=true customs are 422-blocked first by
    ``custom_phi_sms_violations``, so by the time these warnings are computed
    no blocked name remains. De-duplicated, first-seen order.
    """
    if tools is None or tools.sms is None:
        return []
    seen: list[str] = []
    for tmpl in tools.sms.templates:
        for name in _TOKEN_RE.findall(tmpl.body):
            if name not in BUILTIN_NAMES and name not in seen:
                seen.append(name)
    return [
        "{{" + name + "}} is not substituted in SMS — it will render as empty text."
        for name in seen
    ]


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


# --- per-profile policy (Phase A4 spec §3.3.1) ------------------------------
# Quiet-hours times are STRINGS, full stop — "HH:MM" validated by format regex +
# narrowing rules, parsed to datetime.time only inside the validator and at
# consumption (resolve_call_policy). They are never stored as datetime.time on
# the model: the save path persists model_dump() (python mode) into JSONB, where
# a time object raises TypeError — and even mode="json" would round-trip as
# "09:30:00", which the admin-ui zod HH:MM mirror rejects on
# form.reset(profile.draft_config).
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Statutory TCPA bounds (mirrors quiet_hours.QUIET_START_HOUR/QUIET_END_HOUR):
# policy may only NARROW within [09:00, 21:00) local time, never widen.
_STATUTORY_START = time(9, 0)
_STATUTORY_END = time(21, 0)


def _parse_hhmm(value: str) -> time:
    """Parse an already-regex-validated ``"HH:MM"`` string to ``datetime.time``."""
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


class RetryMaxAttempts(BaseModel):
    """Per-status retry caps in CHAIN-GLOBAL attempt semantics (spec §3.3.1).

    ``<status> = N`` means "a call ending with this status schedules a retry iff
    its chain-global attempt number <= N". There are no per-status counters —
    status can change across a chain (no_answer → busy → no_answer), and the
    attempt number is the chain's, not the status's. ``0`` disables retries for
    that status; ``None`` keeps the builtin ladder behavior.
    """

    model_config = ConfigDict(frozen=True)

    # Builtin equivalents below are expressed in the same chain-global semantics
    # (builtin busy: 1 means "retry a busy only when it was the chain's first
    # attempt", not "at most one busy retry ever").
    no_answer: int | None = Field(default=None, ge=0, le=4)  # builtin equivalent: 2
    voicemail_left: int | None = Field(default=None, ge=0, le=4)  # builtin: 1
    busy: int | None = Field(default=None, ge=0, le=4)  # builtin: 1
    failed: int | None = Field(default=None, ge=0, le=4)  # builtin: 1


class PolicyConfig(BaseModel):
    """Optional per-profile quiet-hours narrowing + bounded retry overrides.

    Enforced entirely API-side — re-resolved at every consumption site, never
    snapshotted onto the Call (spec §3.3.2). The agent's AgentConfig mirror
    ignores the riding-along key (pydantic default ``extra="ignore"``).
    """

    model_config = ConfigDict(frozen=True)

    quiet_hours_start_local: str | None = None  # "HH:MM", must be >= "09:00"
    quiet_hours_end_local: str | None = None  # "HH:MM", must be <= "21:00"
    retry_delay_multiplier: float | None = Field(default=None, ge=0.5, le=4.0)
    retry_max_attempts: RetryMaxAttempts | None = None

    @field_validator("quiet_hours_start_local", "quiet_hours_end_local")
    @classmethod
    def _hhmm_format(cls, v: str | None) -> str | None:
        if v is not None and not _HHMM_RE.fullmatch(v):
            raise ValueError('must be "HH:MM" (24-hour clock, minute granularity)')
        return v

    @model_validator(mode="after")
    def _narrowing_only(self) -> PolicyConfig:
        # NARROWING ONLY: each side may be set independently (the unset side
        # stays statutory). next_allowed() does not re-clamp to statutory at
        # consumption — this validator is the gate (spec §7).
        start = (
            _parse_hhmm(self.quiet_hours_start_local)
            if self.quiet_hours_start_local is not None
            else _STATUTORY_START
        )
        end = (
            _parse_hhmm(self.quiet_hours_end_local)
            if self.quiet_hours_end_local is not None
            else _STATUTORY_END
        )
        if start < _STATUTORY_START:
            raise ValueError("quiet_hours_start_local must be at or after the statutory 09:00")
        if end > _STATUTORY_END:
            raise ValueError("quiet_hours_end_local must be at or before the statutory 21:00")
        if start >= end:
            raise ValueError(
                f"quiet_hours_start_local ({start:%H:%M}) must be before "
                f"quiet_hours_end_local ({end:%H:%M})"
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
    # Optional-with-default per the forward-compat invariant above: every
    # published snapshot and older draft (no `policy` key) keeps validating.
    policy: PolicyConfig | None = None


# Defaults below are copied verbatim from the agent's current constants so a new
# profile reproduces today's behavior. Keep in sync if those constants change.
DEFAULT_AGENT_CONFIG = AgentConfig(
    prompts=PromptsConfig(
        system_prompt=(
            "You are a warm, patient daily check-in assistant from USAN Retirement.\n"
            "You are speaking to an contact over the phone. Speak slowly, clearly, and kindly.\n"
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
