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


# Curated allow-list of real, current Cartesia voices chosen for daily wellness
# check-in calls: warm, calm, mature, clear. Each name/description/gender mirrors
# what Cartesia returns for that id, so the picker matches play.cartesia.ai (the
# previous entries were hand-written labels whose ids Cartesia had since repointed
# to different voices — e.g. "Sweet Lady" actually resolved to a young male voice).
# Refreshed 2026-06-14 from the live library; re-verify / extend with
# scripts/list_cartesia_voices.py. Engineers extend it; admins only select from it.
# Ordered for display, with the live-call default (DEFAULT_CARTESIA_VOICE_ID) first.
VOICE_CATALOG: tuple[VoiceSpec, ...] = (
    VoiceSpec(
        cartesia_voice_id="694f9389-aac1-45b6-b726-9d9369183238",
        name="Sarah - Mindful Woman",
        language="en",
        gender="feminine",
        description="Soothing female — calm and reassuring.",
        tts_model_hint="sonic-2",
    ),
    VoiceSpec(
        cartesia_voice_id="9329fbdb-e285-4fba-95ec-592e15f14476",
        name="Rory - Maternal Vibe",
        language="en",
        gender="feminine",
        description="Motherly, nurturing female — warm and reassuring.",
        tts_model_hint="sonic-2",
    ),
    VoiceSpec(
        cartesia_voice_id="3d9b50f9-10c5-4026-9ae1-c4a698f67fc5",
        name="Marjorie - Encouraging Aunt",
        language="en",
        gender="feminine",
        description="Encouraging, matured female with warm reassurance and a steady tone.",
        tts_model_hint="sonic-2",
    ),
    VoiceSpec(
        cartesia_voice_id="0ad65e7f-006c-47cf-bd31-52279d487913",
        name="Rupert - Caring Dad",
        language="en",
        gender="masculine",
        description="Warm, mature male for caring, reassuring conversations.",
        tts_model_hint="sonic-2",
    ),
    VoiceSpec(
        cartesia_voice_id="a924b0e6-9253-4711-8fc3-5cb8e0188c94",
        name="Noah - Calming Presence",
        language="en",
        gender="masculine",
        description="Slow-paced, gentle and soothing male — easy to follow.",
        tts_model_hint="sonic-2",
    ),
    VoiceSpec(
        cartesia_voice_id="9c8880b2-ccf9-4730-b805-cea23df247d7",
        name="Conrad - Seasoned Support",
        language="en",
        gender="masculine",
        description="Mature, confident male with a composed, clear tone.",
        tts_model_hint="sonic-2",
    ),
)

# Membership set for handler-layer save validation (FR-014). A frozenset of the
# catalog's voice ids; an AgentConfig referencing an id outside this set fails to SAVE
# (handler 422) but a previously-published snapshot still deserializes on read.
VOICE_IDS: frozenset[str] = frozenset(v.cartesia_voice_id for v in VOICE_CATALOG)


class VoiceCatalogResponse(BaseModel):
    voices: list[VoiceSpec] = Field(default_factory=list)
