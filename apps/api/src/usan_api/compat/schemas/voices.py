"""RetellAI-compatible catalog schemas (feature 003, US5): the read-only Voice
object and the synthesized concurrency view.

Both are ``extra="allow"`` so the canonical fields can grow toward RetellAI's full
shape (pinned against the captured oracle) without breaking a migrating CRM.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class VoiceProvider(StrEnum):
    """Oracle-pinned provider enum (VoiceResponse.provider)."""

    cartesia = "cartesia"


class CompatVoiceGender(StrEnum):
    """Oracle-pinned gender enum (VoiceResponse.gender).

    Oracle allows only "male" / "female" (non-nullable required field).
    Catalog values "masculine" / "feminine" map to these via _GENDER_MAP in catalog.py.
    """

    male = "male"
    female = "female"


class VoiceResponse(BaseModel):
    """One hosted voice, mapped from the curated catalog (cartesia ⇄ retell alias).

    ``accent``, ``age``, and ``preview_audio_url`` are optional in the oracle schema
    and NOT nullable (type: string, no nullable:true).  They are omitted entirely
    when the curated catalog has no value for them — routes use
    ``response_model_exclude_none=True`` to suppress them from the JSON output.
    """

    model_config = ConfigDict(extra="allow")

    voice_id: str
    voice_name: str
    provider: VoiceProvider
    gender: CompatVoiceGender
    accent: str | None = None
    age: str | None = None
    preview_audio_url: str | None = None


class ConcurrencyResponse(BaseModel):
    """The RetellAI concurrency object synthesized from settings + the live in-flight
    count (data-model §9). The single-VM engine sells no extra concurrency, so the
    ``purchased*`` / ``remaining_purchase_limit`` fields are a static ``0``."""

    model_config = ConfigDict(extra="allow")

    current_concurrency: int
    concurrency_limit: int
    base_concurrency: int
    purchased_concurrency: int = 0
    concurrency_purchase_limit: int = 0
    remaining_purchase_limit: int = 0
    reserved_inbound_concurrency: int
    concurrency_burst_enabled: bool = False
    concurrency_burst_limit: int
