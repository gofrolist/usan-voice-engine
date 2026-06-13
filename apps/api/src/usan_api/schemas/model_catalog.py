"""The curated LLM + STT model catalog (US2 / FR-011–FR-014, research R2).

A GLOBAL code-backed allow-list mirroring ``tool_catalog.py``/``voice_catalog.py``,
exposed read-only via ``GET /v1/admin/model-catalog``. Like the voice catalog it is
NOT a DB table and NOT admin-editable, and stays OUT of the
``agent_profile_versions`` forward-compat invariant: ``LLMConfig.model`` /
``STTConfig.model`` remain plain ``str`` fields (NEVER a ``Literal``/enum), so a
published snapshot referencing a withdrawn model id still deserializes on read.
Selection-from-catalog is validated at the HANDLER layer
(``schemas/agent_config.model_catalog_violations`` + ``routers/admin_profiles.py``),
exactly like ``custom_phi_sms_violations``.

The LLM seeds are all Vertex-served names (provider="vertex"): the agent runs the LLM
on Vertex AI via ADC (Constitution II PHI Containment), so these are deliberately NOT
Gemini Developer API names. STT is Cartesia ``ink-whisper`` (the sole STT model today).
"""

from typing import Literal

from pydantic import BaseModel, Field

ModelKind = Literal["llm", "stt"]


class ModelSpec(BaseModel):
    """One catalog model: stored into AgentConfig.llm.model or .stt.model."""

    id: str  # Stored verbatim into AgentConfig.llm.model / .stt.model.
    label: str  # Display name shown in the picker.
    description: str  # One-line blurb.
    kind: ModelKind  # Filters the picker (llm vs stt).
    provider: str  # e.g. "vertex", "cartesia".
    # Hidden from NEW selection; published configs referencing it still load + render
    # with a deprecation marker.
    deprecated: bool = False
    # Marks the seed default for its kind (the editor may pre-select it for new
    # profiles). Informational only — the AgentConfig field defaults are authoritative.
    default: bool = False


# Seed catalog (research R2). LLM: Vertex Gemini ids (gemini-3.1-flash-lite is the
# current AgentConfig default). STT: Cartesia ink-whisper. Ordered for display.
MODEL_CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec(
        id="gemini-3.1-flash-lite",
        label="Gemini 3.1 Flash Lite",
        description="Fast, low-cost Vertex model — the current default for check-in calls.",
        kind="llm",
        provider="vertex",
        default=True,
    ),
    ModelSpec(
        id="gemini-2.5-flash",
        label="Gemini 2.5 Flash",
        description="Balanced Vertex model — quick responses with stronger reasoning.",
        kind="llm",
        provider="vertex",
    ),
    ModelSpec(
        id="gemini-2.5-flash-lite",
        label="Gemini 2.5 Flash Lite",
        description="Lightweight Vertex model — lowest latency and cost.",
        kind="llm",
        provider="vertex",
    ),
    ModelSpec(
        id="gemini-2.5-pro",
        label="Gemini 2.5 Pro",
        description="Most capable Vertex model — best for complex conversations.",
        kind="llm",
        provider="vertex",
    ),
    ModelSpec(
        id="ink-whisper",
        label="Ink Whisper",
        description="Cartesia streaming speech-to-text model.",
        kind="stt",
        provider="cartesia",
        default=True,
    ),
)

# Per-kind membership sets for handler-layer save validation (FR-014).
LLM_MODEL_NAMES: frozenset[str] = frozenset(m.id for m in MODEL_CATALOG if m.kind == "llm")
STT_MODEL_NAMES: frozenset[str] = frozenset(m.id for m in MODEL_CATALOG if m.kind == "stt")


class ModelCatalogResponse(BaseModel):
    models: list[ModelSpec] = Field(default_factory=list)
