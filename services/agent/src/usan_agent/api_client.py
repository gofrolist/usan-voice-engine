"""Thin JWT-authenticated HTTP client for agent→API calls (design spec §10).

The agent and API share JWT_SIGNING_KEY; the agent mints a short-lived per-call
token so the API can both authenticate the agent and confirm the token is scoped
to the call being mutated.
"""

import time
from typing import Any, Literal, cast

import httpx
import jwt
from loguru import logger

from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig
from usan_agent.ids import validate_call_id as _validate_call_id
from usan_agent.settings import Settings

_TOKEN_TTL_S = 300

# The config fetch is on the call's critical path (before the agent can speak), so use
# a tighter timeout than the 10s tool calls — a slow API must not delay answering.
_CONFIG_TIMEOUT_S = 5.0


def _mint_token(call_id: str, settings: Settings) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + _TOKEN_TTL_S},
        settings.jwt_signing_key,
        algorithm="HS256",
    )


async def report_voicemail_left(call_id: str, settings: Settings) -> None:
    """Best-effort report that a call reached voicemail. Never raises."""
    try:
        call_id = _validate_call_id(call_id)
        url = f"{settings.api_base_url}/v1/calls/{call_id}/outcome"
        headers = {"Authorization": f"Bearer {_mint_token(call_id, settings)}"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json={"outcome": "voicemail_left"}, headers=headers)
            response.raise_for_status()
        logger.bind(call_id=call_id).info("Reported voicemail_left to API")
    except Exception:
        logger.bind(call_id=call_id).warning("Failed to report voicemail_left to API")


async def _post_tool(
    tool: str, call_id: str, settings: Settings, payload: dict[str, Any]
) -> dict[str, Any]:
    """POST a JWT-scoped tool request to the API and return the parsed JSON.

    Raises ValueError on a malformed call_id and httpx.HTTPStatusError on a
    non-2xx response; callers decide how to surface that to the conversation.
    """
    call_id = _validate_call_id(call_id)
    url = f"{settings.api_base_url}/v1/tools/{tool}"
    headers = {"Authorization": f"Bearer {_mint_token(call_id, settings)}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(url, json={"call_id": call_id, **payload}, headers=headers)
        response.raise_for_status()
        return cast(dict[str, Any], response.json())


async def log_wellness(
    call_id: str,
    settings: Settings,
    *,
    mood: int | None,
    pain_level: int | None,
    notes: str | None,
) -> None:
    await _post_tool(
        "log_wellness",
        call_id,
        settings,
        {"mood": mood, "pain_level": pain_level, "notes": notes},
    )


async def flag_for_followup(
    call_id: str,
    settings: Settings,
    *,
    severity: str,
    category: str,
    reason: str,
) -> None:
    await _post_tool(
        "flag_for_followup",
        call_id,
        settings,
        {"severity": severity, "category": category, "reason": reason},
    )


async def log_medication(
    call_id: str,
    settings: Settings,
    *,
    medication_name: str,
    taken: bool,
    reported_time: str | None = None,
) -> None:
    await _post_tool(
        "log_medication",
        call_id,
        settings,
        {"medication_name": medication_name, "taken": taken, "reported_time": reported_time},
    )


async def schedule_callback(
    call_id: str,
    settings: Settings,
    *,
    requested_time_text: str,
    requested_at: str | None,
    notes: str | None,
) -> None:
    await _post_tool(
        "schedule_callback",
        call_id,
        settings,
        {
            "requested_time_text": requested_time_text,
            "requested_at": requested_at,
            "notes": notes,
        },
    )


async def get_today_meds(call_id: str, settings: Settings) -> list[dict[str, Any]]:
    data = await _post_tool("get_today_meds", call_id, settings, {})
    meds = data.get("medications", [])
    return meds if isinstance(meds, list) else []


async def report_end_call(call_id: str, settings: Settings, reason: str) -> None:
    await _post_tool("end_call", call_id, settings, {"reason": reason})


async def flush_transcript(
    call_id: str, settings: Settings, segments: list[dict[str, Any]]
) -> None:
    """Best-effort: POST the call's transcript segments at call end. Never raises."""
    try:
        call_id = _validate_call_id(call_id)
        url = f"{settings.api_base_url}/v1/tools/log_transcript"
        headers = {"Authorization": f"Bearer {_mint_token(call_id, settings)}"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                url, json={"call_id": call_id, "segments": segments}, headers=headers
            )
            response.raise_for_status()
        logger.bind(call_id=call_id).info("Flushed {n} transcript segments", n=len(segments))
    except Exception:
        logger.bind(call_id=call_id).warning("Failed to flush transcript to API")


def _mint_worker_token(settings: Settings) -> str:
    """Mint a worker-scoped token (no call_id) for endpoints that create a call."""
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "iat": now, "exp": now + _TOKEN_TTL_S},
        settings.jwt_signing_key,
        algorithm="HS256",
    )


async def post_metrics(call_id: str, settings: Settings, payload: dict[str, Any]) -> None:
    """Best-effort: POST per-turn latency + per-call usage at call end. Never raises."""
    try:
        call_id = _validate_call_id(call_id)
        url = f"{settings.api_base_url}/v1/tools/log_metrics"
        headers = {"Authorization": f"Bearer {_mint_token(call_id, settings)}"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json={"call_id": call_id, **payload}, headers=headers)
            response.raise_for_status()
        logger.bind(call_id=call_id).info(
            "Posted call metrics: {n} turns", n=len(payload.get("turns", []))
        )
    except Exception:
        logger.bind(call_id=call_id).warning("Failed to post call metrics to API")


async def start_inbound_call(
    phone_e164: str | None,
    livekit_room: str,
    settings: Settings,
    sip_call_id: str | None = None,
) -> dict[str, Any] | None:
    """Best-effort: register an inbound call and fetch elder dynamic vars.

    Returns parsed {call_id, elder_known, dynamic_vars} on success, or None on any
    failure so the worker can fall back to a greet-only inbound conversation.
    """
    url = f"{settings.api_base_url}/v1/calls/inbound"
    headers = {"Authorization": f"Bearer {_mint_worker_token(settings)}"}
    payload = {
        "phone_e164": phone_e164,
        "livekit_room": livekit_room,
        "sip_call_id": sip_call_id,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            return cast(dict[str, Any], response.json())
    except Exception:
        logger.bind(room=livekit_room).warning("Failed to register inbound call with API")
        return None


async def fetch_agent_config(
    settings: Settings, *, direction: Literal["inbound", "outbound"], call_id: str | None = None
) -> AgentConfig:
    """Fetch the resolved agent config; degrade to DEFAULT_AGENT_CONFIG on any failure.

    Best-effort and never raises: a failed config fetch must never crash a call. Uses
    the worker token (matches the server's require_worker_token) and api_base_url
    (so the plaintext-http fail-closed rule holds).
    """
    try:
        url = f"{settings.api_base_url}/v1/runtime/agent-config"
        headers = {"Authorization": f"Bearer {_mint_worker_token(settings)}"}
        params: dict[str, str] = {"direction": direction}
        if call_id:
            params["call_id"] = call_id
        async with httpx.AsyncClient(timeout=_CONFIG_TIMEOUT_S) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            body = response.json()
        return AgentConfig.model_validate(body["config"])
    except Exception as exc:
        # Log the exception TYPE (no body/PHI) so a persistent parse/schema mismatch
        # between the two AgentConfig copies is distinguishable from a transient network
        # blip during triage. Still best-effort: never re-raise (CancelledError is
        # BaseException and is not caught here).
        logger.bind(direction=direction, err=type(exc).__name__).warning(
            "agent-config fetch failed; using defaults"
        )
        # Return a copy so a caller mutating the result can't corrupt the shared singleton.
        return DEFAULT_AGENT_CONFIG.model_copy(deep=True)
