"""The curated voice catalog (US2 / FR-009, FR-010, research R1).

This module is the AUTHORITATIVE platform-curated allow-list of Cartesia voices the
editor offers. It mirrors the ``tool_catalog.py`` catalog-as-code pattern: a GLOBAL
constant (not a DB table, not admin-editable), exposed read-only via
``GET /v1/admin/voice-catalog``. As a global constant it stays OUT of the
``agent_profile_versions`` forward-compat invariant — a published version may
reference a now-``deprecated`` (or even withdrawn) ``cartesia_voice_id`` and still
deserialize on read, because ``VoiceConfig.cartesia_voice_id`` is a plain ``str``
field (NEVER a ``Literal``/enum). Membership is validated at the HANDLER layer
(``schemas/agent_config.voice_violations`` + ``routers/admin_profiles.py``), exactly
like ``custom_phi_sms_violations``.

``SAMPLE_PHRASE`` is a fixed, PHI-free constant used for ALL sample synthesis — it is
never a parameter and never carries contact data, making PHI leakage into the
voice-preview path structurally impossible (Constitution II PHI Containment).
"""

from typing import Literal

from pydantic import BaseModel, Field

# The single, fixed, PHI-free phrase synthesized for every voice preview. NEVER a
# parameter — the sample endpoint hardcodes this constant so no contact name,
# transcript, or other per-call value can ever reach the synthesizer (Constitution II).
SAMPLE_PHRASE: str = (
    "Hello, this is a sample of how this voice sounds on a daily check-in call. "
    "Take care, and have a wonderful day."
)

# Optional gender metadata used only for picker filtering. Kept a Literal here (catalog
# authoring constant) — it does NOT touch the frozen AgentConfig sub-models.
VoiceGender = Literal["masculine", "feminine", "gender_neutral"]


class VoiceSpec(BaseModel):
    """One catalog voice: how the editor describes it and how it is previewed."""

    # The provider voice id stored verbatim into AgentConfig.voice.cartesia_voice_id
    # and passed by the agent into cartesia.TTS(**kwargs) (services/agent/.../pipeline.py).
    cartesia_voice_id: str
    name: str  # Display name shown in the picker.
    language: str  # ISO-639-1 language code (for the picker's language filter).
    # Optional metadata for filtering; None when the provider does not classify it.
    gender: VoiceGender | None = None
    description: str  # Style/character blurb shown in the picker.
    # Suggested TTS model for this voice (a hint the editor may surface; not enforced).
    tts_model_hint: str | None = None
    # Hidden from NEW selection; published configs referencing it still load + render
    # with a deprecation marker (FR-010 deprecation UX).
    deprecated: bool = False


# Seed catalog — a few real Cartesia voices (sonic-2 family). Ordered for display.
# This is the platform allow-list; engineers extend it, admins only select from it.
VOICE_CATALOG: tuple[VoiceSpec, ...] = (
    VoiceSpec(
        cartesia_voice_id="a0e99841-438c-4a64-b679-ae501e7d6091",
        name="Barbershop Man",
        language="en",
        gender="masculine",
        description="Warm, friendly American male — calm and reassuring.",
        tts_model_hint="sonic-2",
    ),
    VoiceSpec(
        cartesia_voice_id="729651dc-c6c3-4ee5-97fa-350da1f88600",
        name="Sweet Lady",
        language="en",
        gender="feminine",
        description="Gentle, patient American female — clear and kind.",
        tts_model_hint="sonic-2",
    ),
    VoiceSpec(
        cartesia_voice_id="a167e0f3-df7e-4d52-a9c3-f949145efdab",
        name="Friendly Reading Man",
        language="en",
        gender="masculine",
        description="Slow, articulate narration voice — good for instructions.",
        tts_model_hint="sonic-2",
    ),
    VoiceSpec(
        cartesia_voice_id="b7d50908-b17c-442d-ad8d-810c63997ed9",
        name="Calm Lady",
        language="en",
        gender="feminine",
        description="Soothing, even-paced American female — easy to follow.",
        tts_model_hint="sonic-2",
    ),
)

# Membership set for handler-layer save validation (FR-014). A frozenset of the
# catalog's voice ids; an AgentConfig referencing an id outside this set fails to SAVE
# (handler 422) but a previously-published snapshot still deserializes on read.
VOICE_IDS: frozenset[str] = frozenset(v.cartesia_voice_id for v in VOICE_CATALOG)


class VoiceCatalogResponse(BaseModel):
    voices: list[VoiceSpec] = Field(default_factory=list)
