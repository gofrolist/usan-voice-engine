"""Modeled per-call cost: usage × versioned pricing constants (design spec §6).

Pure and DB-free so it unit-tests without a database. All money is Decimal,
quantized to 6 dp to match the NUMERIC(12,6) columns.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

_Q = Decimal("0.000001")


@dataclass(frozen=True)
class Pricing:
    telnyx_per_min: Decimal
    llm_in_per_1k: Decimal
    llm_out_per_1k: Decimal
    stt_per_min: Decimal
    tts_per_1k_chars: Decimal
    gcs_per_gb_month: Decimal
    version: str

    @classmethod
    def from_settings(cls, settings: Any) -> Pricing:
        return cls(
            telnyx_per_min=settings.telnyx_per_min_usd,
            llm_in_per_1k=settings.llm_input_per_1k_usd,
            llm_out_per_1k=settings.llm_output_per_1k_usd,
            stt_per_min=settings.cartesia_stt_per_min_usd,
            tts_per_1k_chars=settings.cartesia_tts_per_1k_chars_usd,
            gcs_per_gb_month=settings.gcs_storage_per_gb_month_usd,
            version=settings.pricing_version,
        )


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value or 0))


def compute_costs(
    *,
    duration_seconds: int | None,
    llm_prompt_tokens: int,
    llm_completion_tokens: int,
    tts_characters: int,
    stt_audio_seconds: float,
    recording_bytes: int,
    pricing: Pricing,
) -> dict[str, Decimal]:
    telephony = _d(duration_seconds) / Decimal(60) * pricing.telnyx_per_min
    llm = (
        _d(llm_prompt_tokens) / Decimal(1000) * pricing.llm_in_per_1k
        + _d(llm_completion_tokens) / Decimal(1000) * pricing.llm_out_per_1k
    )
    stt = _d(stt_audio_seconds) / Decimal(60) * pricing.stt_per_min
    tts = _d(tts_characters) / Decimal(1000) * pricing.tts_per_1k_chars
    storage = _d(recording_bytes) / Decimal(1_000_000_000) * pricing.gcs_per_gb_month
    parts = {
        "telephony": telephony.quantize(_Q),
        "llm": llm.quantize(_Q),
        "stt": stt.quantize(_Q),
        "tts": tts.quantize(_Q),
        "storage": storage.quantize(_Q),
    }
    parts["total"] = sum(parts.values()).quantize(_Q)
    return parts
