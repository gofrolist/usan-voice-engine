"""Voice catalog + audio-preview proxy (US2 / FR-009, FR-010, research R1).

``GET /v1/admin/voice-catalog`` returns the curated voice allow-list (mirrors
``admin_tool_catalog``). ``GET /v1/admin/voice-catalog/{voice_id}/sample`` is a
server-side proxy that lazily synthesizes a FIXED, PHI-free phrase
(``voice_catalog.SAMPLE_PHRASE``) via Cartesia ``POST /tts/bytes`` and streams the
audio back as ``audio/mpeg``. The bytes are cached per ``(voice_id, model)`` so each
voice is synthesized at most once.

Security & PHI:
- The Cartesia secret (``CARTESIA_API_KEY``) stays server-side: the browser never calls
  Cartesia directly. The proxy is operator-only (super-admin) and rate-limited.
- ONLY ``SAMPLE_PHRASE`` reaches the synthesizer — it is a module constant, never a
  request parameter — so no contact name/transcript can leak into the sample path
  (Constitution II PHI Containment).
- 404 if the voice id is not in the curated catalog; 503 if the API key is unset.
"""

from collections.abc import Iterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from loguru import logger

from usan_api.auth import require_super_admin
from usan_api.schemas.voice_catalog import (
    SAMPLE_PHRASE,
    VOICE_CATALOG,
    VOICE_IDS,
    VoiceCatalogResponse,
)
from usan_api.settings import Settings, get_settings

router = APIRouter(
    prefix="/v1/admin/voice-catalog",
    tags=["admin-voice-catalog"],
    dependencies=[Depends(require_super_admin)],
)

# Module-level cache of synthesized sample bytes, keyed by (voice_id, model). The
# catalog is a global constant and SAMPLE_PHRASE never changes, so a voice's sample is
# stable for the process lifetime — cache it to avoid repeated TTS spend (research R1).
_SAMPLE_CACHE: dict[tuple[str, str], bytes] = {}


@router.get("", response_model=VoiceCatalogResponse)
async def get_voice_catalog() -> VoiceCatalogResponse:
    """Return the curated voice catalog for the VoiceSection picker (FR-009).

    Operator-only (super-admin) scope, mirroring admin_tool_catalog. The catalog is a
    global constant (a platform-curated allow-list), not per-version snapshot data.
    """
    return VoiceCatalogResponse(voices=list(VOICE_CATALOG))


async def _synthesize_sample(settings: Settings, voice_id: str, model: str) -> bytes:
    """POST the fixed SAMPLE_PHRASE to Cartesia /tts/bytes and return the MP3 bytes.

    Caches per (voice_id, model). On an upstream failure it logs the real Cartesia
    status + body (the only signal for *why* the preview failed) and raises a clean,
    actionable HTTPException: a 402 "credit limit reached" becomes a 503 telling the
    operator to check the Cartesia subscription; any other upstream/transport failure
    becomes a 502 naming the upstream status — never a stack trace.
    """
    cache_key = (voice_id, model)
    cached = _SAMPLE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    api_key = settings.cartesia_api_key
    assert api_key is not None  # caller checked presence (503 otherwise)
    url = settings.cartesia_api_url.rstrip("/") + "/tts/bytes"
    headers = {
        "Authorization": f"Bearer {api_key.get_secret_value()}",
        "Cartesia-Version": settings.cartesia_version,
        "Content-Type": "application/json",
    }
    # ONLY SAMPLE_PHRASE reaches synthesis — a fixed constant, never a parameter.
    payload = {
        "model_id": model,
        "transcript": SAMPLE_PHRASE,
        "voice": {"mode": "id", "id": voice_id},
        "output_format": {
            "container": "mp3",
            "sample_rate": 44100,
            "bit_rate": 128000,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            audio = resp.content
    except httpx.HTTPStatusError as exc:
        upstream = exc.response.status_code
        # SAMPLE_PHRASE is PHI-free, so Cartesia's error body is safe to log — and it is
        # the only signal that explains the failure (e.g. an exhausted credit balance).
        logger.warning(
            "Cartesia voice-sample synthesis failed: voice={voice} model={model} "
            "upstream={status} body={body}",
            voice=voice_id,
            model=model,
            status=upstream,
            body=exc.response.text[:500],
        )
        if upstream == status.HTTP_402_PAYMENT_REQUIRED:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "voice preview unavailable: the Cartesia account has reached its "
                    "credit limit — check the Cartesia subscription"
                ),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"voice sample synthesis failed (Cartesia returned {upstream})",
        ) from exc
    except httpx.HTTPError as exc:
        logger.warning(
            "Cartesia voice-sample request error: voice={voice} model={model} err={err}",
            voice=voice_id,
            model=model,
            err=repr(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="voice sample synthesis failed (Cartesia unreachable)",
        ) from exc
    _SAMPLE_CACHE[cache_key] = audio
    return audio


@router.get("/{voice_id}/sample")
async def get_voice_sample(
    voice_id: str,
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    """Stream a fixed PHI-free sample of ``voice_id`` as audio/mpeg (FR-010).

    404 when the voice is not in the curated catalog; 503 when the Cartesia API key is
    not configured (the proxy cannot synthesize without it).
    """
    if voice_id not in VOICE_IDS:
        raise HTTPException(status_code=404, detail="voice not found")
    if settings.cartesia_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="voice preview is not configured (CARTESIA_API_KEY unset)",
        )
    audio = await _synthesize_sample(settings, voice_id, settings.cartesia_sample_model)

    def _iter() -> Iterator[bytes]:
        yield audio

    return StreamingResponse(_iter(), media_type="audio/mpeg")
