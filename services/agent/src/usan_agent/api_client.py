"""Thin JWT-authenticated HTTP client for agent→API calls (design spec §10).

The agent and API share JWT_SIGNING_KEY; the agent mints a short-lived per-call
token so the API can both authenticate the agent and confirm the token is scoped
to the call being mutated.
"""

import time

import httpx
import jwt
from loguru import logger

from usan_agent.settings import Settings

_TOKEN_TTL_S = 300


def _mint_token(call_id: str, settings: Settings) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + _TOKEN_TTL_S},
        settings.jwt_signing_key,
        algorithm="HS256",
    )


async def report_voicemail_left(call_id: str, settings: Settings) -> None:
    """Best-effort report that a call reached voicemail. Never raises."""
    url = f"{settings.api_base_url}/v1/calls/{call_id}/outcome"
    headers = {"Authorization": f"Bearer {_mint_token(call_id, settings)}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json={"outcome": "voicemail_left"}, headers=headers)
            response.raise_for_status()
        logger.bind(call_id=call_id).info("Reported voicemail_left to API")
    except Exception:
        logger.bind(call_id=call_id).warning("Failed to report voicemail_left to API")
