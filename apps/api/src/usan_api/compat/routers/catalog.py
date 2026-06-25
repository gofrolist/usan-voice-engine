"""RetellAI-compatible read-only catalog endpoints (feature 003, US5):

  GET /list-voices                 (bare array of hosted voices)
  GET /get-voice/{voice_id}        (retell alias OR raw cartesia id; 404 if unhosted)
  GET /get-concurrency             (synthesized from settings + live in-flight count)

Auth + org-scoped RLS session via ``get_compat_db``; one PHI-free audit line per op.
Voices come from the curated ``VOICE_CATALOG`` (a global constant), mapped to RetellAI
ids by ``voice_map``. ``get-concurrency`` reads the org's in-flight count under RLS.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat.auth import get_compat_db
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.voices import (
    CompatVoiceGender,
    ConcurrencyResponse,
    VoiceProvider,
    VoiceResponse,
)
from usan_api.compat.voice_map import resolve_voice_id, to_retell_voice_id
from usan_api.repositories import calls as calls_repo
from usan_api.schemas.voice_catalog import VOICE_CATALOG, VoiceSpec
from usan_api.settings import Settings, get_settings

router = APIRouter(tags=["compat-catalog"])

# Curated gender (feminine/masculine) -> oracle-pinned CompatVoiceGender enum.
_GENDER_MAP: dict[str, CompatVoiceGender] = {
    "feminine": CompatVoiceGender.female,
    "masculine": CompatVoiceGender.male,
}


def _audit(request: Request, op: str) -> None:
    org = getattr(request.state, "compat_org_id", None)
    logger.bind(compat_org_id=org, op=op).info("compat catalog op={op}")


def _serialize_voice(spec: VoiceSpec) -> VoiceResponse:
    gender = _GENDER_MAP.get(spec.gender or "")
    if gender is None:
        raise ValueError(
            f"voice {spec.cartesia_voice_id!r} has unmappable gender {spec.gender!r}; "
            "only 'masculine' and 'feminine' map to oracle VoiceGender (CompatVoiceGender)"
        )
    return VoiceResponse(
        # Catalog ids always map; fall back to the raw id so voice_id is never null.
        voice_id=to_retell_voice_id(spec.cartesia_voice_id) or spec.cartesia_voice_id,
        voice_name=spec.name,
        provider=VoiceProvider.cartesia,
        gender=gender,
    )


def _find_spec(voice_id: str) -> VoiceSpec | None:
    """Resolve a RetellAI ``voice_id`` (alias or raw cartesia id) to its catalog spec,
    including deprecated voices (a live config may still reference one). ``None`` =
    not a hosted voice."""
    try:
        cartesia = resolve_voice_id(voice_id)
    except CompatError:
        return None
    return next((s for s in VOICE_CATALOG if s.cartesia_voice_id == cartesia), None)


@router.get("/list-voices", response_model=list[VoiceResponse], response_model_exclude_none=True)
async def list_voices(request: Request) -> list[VoiceResponse]:
    """A BARE array of hosted voices. Deprecated voices are hidden from this
    new-selection list (matching the admin-UI picker); ``get-voice`` still resolves
    them so an in-use config can be read back."""
    _audit(request, "list-voices")
    return [_serialize_voice(s) for s in VOICE_CATALOG if not s.deprecated]


@router.get("/get-voice/{voice_id}", response_model=VoiceResponse, response_model_exclude_none=True)
async def get_voice(voice_id: str, request: Request) -> VoiceResponse:
    spec = _find_spec(voice_id)
    if spec is None:
        raise CompatError(404, f"voice '{voice_id}' not found")
    _audit(request, "get-voice")
    return _serialize_voice(spec)


@router.get("/get-concurrency", response_model=ConcurrencyResponse)
async def get_concurrency(
    request: Request,
    db: AsyncSession = Depends(get_compat_db),
    settings: Settings = Depends(get_settings),
) -> ConcurrencyResponse:
    # Live in-flight (non-terminal, recency-bounded) count for THIS org (RLS-scoped),
    # exactly as the dialer concurrency gate measures it.
    current = await calls_repo.count_in_flight(
        db, now=datetime.now(UTC), max_age_s=settings.outbound_max_call_duration_s + 120
    )
    limit = settings.max_concurrent_calls
    _audit(request, "get-concurrency")
    return ConcurrencyResponse(
        current_concurrency=current,
        concurrency_limit=limit,
        base_concurrency=limit,
        reserved_inbound_concurrency=settings.reserved_concurrency,
        concurrency_burst_limit=limit,
    )
