"""Compat (RetellAI) webhook delivery poller (feature 003 / US2, T028).

A SEPARATE lifespan poller from the native ``webhook_delivery`` (which signs X-Usan and
claims ALL due rows). It mirrors the native worker's proven discipline — claim-as-lease (no
locks across POSTs), per-row short transactions, per-endpoint circuit breaker, the
crash-residue/expire/prune housekeeping — but over the ``compat_webhook_*`` tables, and:

- builds the FULL ``{event, call}`` body from the LIVE Call at delivery time (off the hot
  transition path; latest state) via ``compat.call_serializer``;
- signs the Retell ``x-retell-signature`` scheme with the subscription's dedicated secret
  (sign-what-you-send: the digest is over the exact POSTed bytes, fresh ms timestamp);
- carries the dedupe id in the ``x-retell-delivery-id`` HEADER, keeping the body a
  byte-faithful RetellAI ``{event, call}``;
- enforces the ``COMPAT_WEBHOOK_ALLOWED_HOSTS`` allow-list AND ``ssrf_guard`` before every
  POST (PHI never leaves to a non-allow-listed host; FR-022/SC-005).

Like the native poller the task ALWAYS starts; ``COMPAT_WEBHOOK_DELIVERY_ENABLED`` gates only
the claim+POST half — housekeeping + the pending gauge run flag-independently (ship-inert).

RLS/single-org: the poller opens sessions via ``get_session_factory()`` and so runs under the
default-org connect-baseline (db.session._install_default_org_context), exactly like the
native poller. Multi-org compat delivery is therefore bounded by the SAME single-org-runtime
deferral that governs the call plane; revisit when the runtime plane goes multi-org.

PHI/log rules: bind ids only (delivery_id, endpoint_id, event) — NEVER the webhook_url, the
secret, or ``str(exc)``; ``last_error`` is the exception TYPE NAME only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usan_api import ssrf_guard
from usan_api.compat import call_serializer, webhook_signature
from usan_api.db.models import Call, CompatWebhookEndpoint
from usan_api.db.session import get_session_factory
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import compat_webhooks as repo
from usan_api.settings import Settings
from usan_api.ssrf_guard import SsrfBlocked

_HOUSEKEEPING_INTERVAL_S = 3600.0
_USER_AGENT = "usan-voice-engine-compat-webhooks/1.0"

_OUTCOME_DELIVERED = "delivered"
_OUTCOME_RETRY = "retry_scheduled"
_OUTCOME_FAILED = "failed"
_OUTCOME_SSRF = "ssrf_blocked"
_OUTCOME_SKIPPED = "skipped"
_OUTCOME_GONE = "call_gone"


def _housekeeping_due(last_run: float | None, now: float) -> bool:
    return last_run is None or now - last_run >= _HOUSEKEEPING_INTERVAL_S


def _build_client(settings: Settings) -> httpx.AsyncClient:
    """Client seam for tests. ``follow_redirects=False`` is load-bearing (redirect-to-internal
    SSRF: a 3xx is a failure, never followed)."""
    return httpx.AsyncClient(timeout=settings.webhook_delivery_timeout_s, follow_redirects=False)


async def _guard_host(host: str, allowed: frozenset[str]) -> list[str]:
    """Delivery-time PHI gate: the host MUST be in the allow-list (empty list => nothing
    leaves) AND globally routable. Returns the validated addresses so the caller can pin
    the connection to a vetted IP. Defense-in-depth — registration already gates the host,
    but the allow-list may have shrunk since."""
    if not allowed or host.lower() not in allowed:
        raise SsrfBlocked("compat webhook host not in COMPAT_WEBHOOK_ALLOWED_HOSTS")
    return await ssrf_guard.resolve_public_or_raise(host)


async def _build_body(
    db: AsyncSession, settings: Settings, call: Call, event: str, *, client_host: str
) -> dict[str, object]:
    """The byte-faithful RetellAI webhook body: ``{event, call: <full Call object>}``."""
    compat_call = await call_serializer.serialize_call(db, call, settings, client_host=client_host)
    return {"event": event, "call": compat_call.model_dump(mode="json", exclude_none=True)}


async def deliver_one(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    claimed: repo.ClaimedCompatDelivery,
    client: httpx.AsyncClient,
) -> str:
    """Deliver one claimed row in its own short transaction.

    Returns delivered|retry_scheduled|failed|ssrf_blocked|skipped|call_gone.
    """
    log = logger.bind(
        component="compat_webhook_delivery",
        delivery_id=str(claimed.id),
        endpoint_id=str(claimed.endpoint_id),
        event=claimed.event,
    )
    async with factory() as db:
        endpoint = await db.get(CompatWebhookEndpoint, claimed.endpoint_id)
        if endpoint is None or not endpoint.enabled:
            return _OUTCOME_SKIPPED
        url = endpoint.webhook_url
        secret = endpoint.secret
        host = urlsplit(url).hostname or ""

        # The Call is the source of truth for the body; if it is gone the delivery can never
        # succeed — settle it terminally now rather than burning the retry ladder.
        call = await calls_repo.get_call(db, uuid.UUID(str(claimed.payload["call_id"])))
        if call is None:
            await repo.mark_attempt_failed(
                db, claimed.id, response_code=None, last_error="CallNotFound", terminal=True
            )
            await db.commit()
            log.bind(outcome=_OUTCOME_GONE).info("Compat webhook delivery attempt settled")
            return _OUTCOME_GONE

        response_code: int | None = None
        try:
            addrs = await _guard_host(host, settings.compat_webhook_allowed_hosts_set)
            body = await _build_body(db, settings, call, claimed.event, client_host=host)
            raw = json.dumps(body, separators=(",", ":")).encode()
            ts_ms = int(time.time() * 1000)
            headers = {
                "Content-Type": "application/json",
                "User-Agent": _USER_AGENT,
                "x-retell-delivery-id": str(claimed.id),
                "x-retell-signature": webhook_signature.signature_header(
                    ts_ms, webhook_signature.sign(secret, raw, ts_ms)
                ),
            }
            # addrs is non-empty: resolve_public_or_raise raises on empty resolution.
            pinned = ssrf_guard.pin_request(url, addrs[0], headers)
            async with client.stream(
                "POST",
                pinned.url,
                content=raw,
                headers=pinned.headers,
                extensions=pinned.extensions,
            ) as response:
                response_code = response.status_code
                drained = 0
                # Wall-clock deadline: the httpx read timeout resets per chunk, so a slow-drip
                # receiver could hold this slot open past the timeout. TimeoutError settles via
                # the OSError branch below.
                deadline = time.monotonic() + settings.webhook_delivery_timeout_s
                async for chunk in response.aiter_bytes():
                    drained += len(chunk)
                    if drained >= settings.webhook_delivery_max_response_bytes:
                        break
                    if time.monotonic() >= deadline:
                        raise TimeoutError("webhook response exceeded delivery timeout")
                response.raise_for_status()
        except (httpx.HTTPError, OSError, ValueError, SsrfBlocked) as exc:
            terminal = claimed.attempts >= 4
            await repo.mark_attempt_failed(
                db,
                claimed.id,
                response_code=response_code,
                last_error=type(exc).__name__,
                terminal=terminal,
            )
            failures = await repo.increment_failures(db, claimed.endpoint_id)
            tripped = False
            if (
                failures is not None
                and failures >= settings.webhook_delivery_circuit_breaker_threshold
            ):
                tripped = await repo.trip_breaker(db, claimed.endpoint_id)
            await db.commit()
            if tripped:
                logger.bind(
                    component="compat_webhook_delivery", endpoint_id=str(claimed.endpoint_id)
                ).warning("Compat webhook endpoint auto-disabled by circuit breaker")
            # Label SSRF first so a *final-attempt* block still reports ``ssrf_blocked`` (the
            # PHI-gate signal) instead of being folded into the generic ``failed`` count. The
            # row is still settled terminally above via the ``terminal`` flag — only the
            # outcome/stat label changes here.
            if isinstance(exc, SsrfBlocked):
                outcome = _OUTCOME_SSRF
            elif terminal:
                outcome = _OUTCOME_FAILED
            else:
                outcome = _OUTCOME_RETRY
            log.bind(outcome=outcome, response_code=response_code).info(
                "Compat webhook delivery attempt settled"
            )
            return outcome

        await repo.mark_delivered(db, claimed.id, response_code=response_code)
        await repo.reset_failures(db, claimed.endpoint_id)
        await db.commit()
        log.bind(outcome=_OUTCOME_DELIVERED, response_code=response_code).info(
            "Compat webhook delivery attempt settled"
        )
        return _OUTCOME_DELIVERED


async def _deliver_group(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    rows: list[repo.ClaimedCompatDelivery],
    client: httpx.AsyncClient,
) -> dict[str, int]:
    """One endpoint's claimed rows, sequential oldest-first."""
    counts: dict[str, int] = {}
    for claimed in rows:
        outcome = await deliver_one(factory, settings, claimed, client)
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome == _OUTCOME_SKIPPED:
            break  # breaker tripped mid-cycle: every later row in this group would skip too
    return counts


async def poll_once(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    now: datetime | None = None,
    run_housekeeping: bool = False,
) -> dict[str, int]:
    """One poller cycle. Returns per-outcome + housekeeping + backlog stats."""
    now = now if now is not None else datetime.now(UTC)
    stats = {
        "pending": 0,
        _OUTCOME_DELIVERED: 0,
        _OUTCOME_RETRY: 0,
        _OUTCOME_FAILED: 0,
        _OUTCOME_SSRF: 0,
        _OUTCOME_SKIPPED: 0,
        _OUTCOME_GONE: 0,
        "swept": 0,
        "expired": 0,
        "pruned": 0,
    }

    # EVERY-CYCLE half (flag-independent): backlog depth.
    async with factory() as db:
        stats["pending"] = await repo.count_pending(db)

    # HOURLY half (flag-independent): sweep/expire/prune in one txn.
    if run_housekeeping:
        async with factory() as db:
            stats["swept"] = await repo.sweep_crash_residue(db, now=now)
            stats["expired"] = await repo.expire_stale_pending(db, now=now)
            stats["pruned"] = await repo.prune_old(db, now=now)
            await db.commit()

    # Delivery half — the ONLY part the flag gates.
    if not settings.compat_webhook_delivery_enabled:
        return stats

    async with factory() as db:
        claimed = await repo.claim_due(db, now=now)
        await db.commit()
    if not claimed:
        return stats

    groups: dict[uuid.UUID, list[repo.ClaimedCompatDelivery]] = {}
    for row in claimed:
        groups.setdefault(row.endpoint_id, []).append(row)

    async with _build_client(settings) as client:
        results = await asyncio.gather(
            *(_deliver_group(factory, settings, rows, client) for rows in groups.values()),
            return_exceptions=True,
        )
    for endpoint_id, counts in zip(groups, results, strict=True):
        if isinstance(counts, BaseException):
            logger.bind(
                component="compat_webhook_delivery",
                endpoint_id=str(endpoint_id),
                err=type(counts).__name__,
            ).error("Compat webhook delivery group failed")
            continue
        for outcome, count in counts.items():
            stats[outcome] += count
    return stats


async def run_poller(settings: Settings, stop: asyncio.Event) -> None:
    """Loop ``poll_once`` until ``stop`` is set (native poller loop discipline; per-cycle
    exceptions log ``type(exc).__name__`` only — never a traceback that could embed a URL)."""
    log = logger.bind(component="compat_webhook_delivery")
    log.info(
        "Compat webhook delivery poller started (interval={i}s, delivery_enabled={e})",
        i=settings.webhook_delivery_poll_interval_s,
        e=settings.compat_webhook_delivery_enabled,
    )
    factory = get_session_factory()
    last_housekeeping: float | None = None
    while not stop.is_set():
        try:
            now_mono = time.monotonic()
            run_housekeeping = _housekeeping_due(last_housekeeping, now_mono)
            stats = await poll_once(factory, settings, run_housekeeping=run_housekeeping)
            if run_housekeeping:
                last_housekeeping = now_mono
            if any(value for key, value in stats.items() if key != "pending"):
                log.info("Compat webhook delivery cycle work: {stats}", stats=stats)
        except Exception as exc:
            log.bind(err=type(exc).__name__).error("Compat webhook delivery poll cycle failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.webhook_delivery_poll_interval_s)
    log.info("Compat webhook delivery poller stopped")
