"""Surface 2A: synchronous inbound-call routing egress (design 2026-07-09).

On each inbound call, ``register_inbound_call`` asks the client's ``inbound-call-router`` who to
be: we POST ``{event: "call_inbound", call_inbound: {from_number, to_number}}`` and they reply
``{call_inbound: {override_agent_id, dynamic_variables}}`` (migration spec §3, byte-for-byte).

This module owns only the egress + parse. It NEVER raises: every failure path (flag off, no URL,
network, non-2xx, redirect, bad JSON, missing ``call_inbound`` wrapper) returns ``None`` so the
caller degrades to the DID's default inbound agent — an inbound call must always connect. The URL
is a single operator-configured endpoint (its host is the allow-list), but the SSRF public-IP pin
still applies fail-closed against a misconfigured internal URL, mirroring the webhook sender.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from loguru import logger

from usan_api import ssrf_guard
from usan_api.settings import Settings

# Blocks inbound call setup — the caller hears silence during this round-trip, so keep it tight.
_INBOUND_ROUTER_TIMEOUT_S = 5.0
# The client's router response is a tiny JSON object; cap the read so a misbehaving endpoint
# can't stream unbounded bytes into call setup.
_MAX_RESPONSE_BYTES = 64 * 1024


def _build_client() -> httpx.AsyncClient:
    """Client seam for tests (MockTransport). follow_redirects=False is load-bearing: a 3xx to an
    attacker-chosen Location is a failure, never followed (mirrors the webhook sender)."""
    return httpx.AsyncClient(follow_redirects=False)


@dataclass(frozen=True)
class InboundRouterResult:
    """Parsed ``call_inbound`` decision. ``override_agent_id`` is a Retell-style ``agent_<hex>``
    token (decoded + published-checked by the caller); ``dynamic_variables`` are string vars."""

    override_agent_id: str
    dynamic_variables: dict[str, str]


def _with_caller_secret(url: str, secret: str | None) -> str:
    """Append ``?caller_secret=`` when a secret is configured (the inbound webhook has no header
    slot — migration spec §3). Preserves any existing query params."""
    if not secret:
        return url
    parts = urlsplit(url)
    query = parts.query + ("&" if parts.query else "") + urlencode({"caller_secret": secret})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _parse(body: Any) -> InboundRouterResult | None:
    """Parse the strict ``{call_inbound: {override_agent_id, dynamic_variables}}`` shape. The old
    flat ``{agent_id, retell_llm_dynamic_variables}`` shape is intentionally NOT accepted (Retell
    ignored it — migration spec §3). Any deviation → ``None`` (degrade)."""
    if not isinstance(body, dict):
        return None
    inner = body.get("call_inbound")
    if not isinstance(inner, dict):
        return None
    agent_id = inner.get("override_agent_id")
    if not isinstance(agent_id, str) or not agent_id.strip():
        return None
    raw_vars = inner.get("dynamic_variables")
    # Values are strings per spec; coerce defensively so a stray non-string can't break the agent.
    dynamic_vars = (
        {str(k): str(v) for k, v in raw_vars.items()} if isinstance(raw_vars, dict) else {}
    )
    return InboundRouterResult(override_agent_id=agent_id.strip(), dynamic_variables=dynamic_vars)


async def route_inbound(
    settings: Settings, *, from_number: str | None, to_number: str | None
) -> InboundRouterResult | None:
    """Ask the client's inbound-call-router who to be. Returns the decision, or ``None`` on any
    failure or when the feature is off — the caller then degrades to the default inbound agent."""
    url = settings.compat_inbound_router_url
    if not settings.compat_inbound_router_enabled or not url:
        return None
    host = urlsplit(url).hostname or ""
    payload = {
        "event": "call_inbound",
        "call_inbound": {"from_number": from_number, "to_number": to_number},
    }
    try:
        addrs = await ssrf_guard.resolve_public_or_raise(host)
        pinned = ssrf_guard.pin_request(
            _with_caller_secret(url, settings.compat_inbound_router_caller_secret),
            addrs[0],
            {"Content-Type": "application/json"},
        )
        raw = json.dumps(payload, separators=(",", ":")).encode()
        chunks = bytearray()
        async with (
            _build_client() as client,
            client.stream(
                "POST",
                pinned.url,
                content=raw,
                headers=pinned.headers,
                extensions=pinned.extensions,
                timeout=_INBOUND_ROUTER_TIMEOUT_S,
            ) as resp,
        ):
            async for chunk in resp.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) >= _MAX_RESPONSE_BYTES:
                    break
            resp.raise_for_status()
        return _parse(json.loads(bytes(chunks[:_MAX_RESPONSE_BYTES])))
    except (httpx.HTTPError, OSError, ValueError, ssrf_guard.SsrfBlocked) as exc:
        # PHI-free: exception type + status only — never the numbers, response, or dynamic vars.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        logger.bind(err=type(exc).__name__, status=status).warning("inbound router call failed")
        return None
