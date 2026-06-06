from decimal import Decimal

from usan_api.cost import Pricing, compute_costs

_ZERO = dict(
    llm_in_per_1k=Decimal("0"),
    llm_out_per_1k=Decimal("0"),
    stt_per_min=Decimal("0"),
    tts_per_1k_chars=Decimal("0"),
    gcs_per_gb_month=Decimal("0"),
    version="t",
)


def test_telephony_only():
    pricing = Pricing(telnyx_per_min=Decimal("0.008"), **_ZERO)
    costs = compute_costs(
        duration_seconds=120,
        llm_prompt_tokens=0,
        llm_completion_tokens=0,
        tts_characters=0,
        stt_audio_seconds=0,
        recording_bytes=0,
        pricing=pricing,
    )
    assert costs["telephony"] == Decimal("0.016000")
    assert costs["total"] == Decimal("0.016000")


def test_all_components_sum():
    pricing = Pricing(
        telnyx_per_min=Decimal("0.006"),
        llm_in_per_1k=Decimal("0.10"),
        llm_out_per_1k=Decimal("0.40"),
        stt_per_min=Decimal("0.02"),
        tts_per_1k_chars=Decimal("0.05"),
        gcs_per_gb_month=Decimal("0"),
        version="t",
    )
    costs = compute_costs(
        duration_seconds=60,
        llm_prompt_tokens=1000,
        llm_completion_tokens=500,
        tts_characters=2000,
        stt_audio_seconds=60,
        recording_bytes=0,
        pricing=pricing,
    )
    assert costs["telephony"] == Decimal("0.006000")
    assert costs["llm"] == Decimal("0.300000")
    assert costs["stt"] == Decimal("0.020000")
    assert costs["tts"] == Decimal("0.100000")
    assert costs["total"] == Decimal("0.426000")


def test_none_duration_is_zero():
    pricing = Pricing(telnyx_per_min=Decimal("0.008"), **_ZERO)
    costs = compute_costs(
        duration_seconds=None,
        llm_prompt_tokens=0,
        llm_completion_tokens=0,
        tts_characters=0,
        stt_audio_seconds=0.0,
        recording_bytes=0,
        pricing=pricing,
    )
    assert costs["total"] == Decimal("0.000000")
