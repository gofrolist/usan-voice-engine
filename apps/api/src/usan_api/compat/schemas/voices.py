"""RetellAI-compatible catalog schemas (feature 003, US5): the read-only Voice
object and the synthesized concurrency view.

Both are ``extra="allow"`` so the canonical fields can grow toward RetellAI's full
shape (pinned against the captured oracle) without breaking a migrating CRM.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class VoiceResponse(BaseModel):
    """One hosted voice, mapped from the curated catalog (cartesia ⇄ retell alias).
    ``accent``/``age``/``preview_audio_url`` are null — the curated catalog does not
    track them (PENDING-FREEZE: enrich against the captured CRM oracle if needed)."""

    model_config = ConfigDict(extra="allow")

    voice_id: str
    voice_name: str
    provider: str
    accent: str | None = None
    gender: str | None = None
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
