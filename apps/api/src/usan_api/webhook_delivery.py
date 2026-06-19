"""Outbound webhook delivery worker — the 4th lifespan poller (spec §5).

Design (spec §5.2–§5.5): the claim is an attempt-bump LEASE committed
immediately, so no row locks are ever held across HTTP POSTs and a crash
anywhere after the claim-commit simply re-offers the row at its next ladder
rung (at-least-once; receivers dedupe on the signed body ``delivery_id``).
Claimed rows are grouped by endpoint and groups deliver CONCURRENTLY
(``asyncio.gather``), sequential oldest-first within a group, so a hanging
receiver delays only its own group. Each row's outcome commits in its own
short transaction (sms_outbox discipline), bounding the duplicate window to
one row. Per-endpoint ``consecutive_failures`` feeds the circuit breaker;
the trip is a guarded one-shot that auto-disables the endpoint (operator
re-enable is the only recovery).

The poller task itself is ALWAYS started; ``WEBHOOK_DELIVERY_ENABLED`` gates
only the claim+POST half of each cycle. The housekeeping half (crash-residue
sweep, 7-day pending expiry, 30-day prune — hourly) and the pending-backlog
count (every cycle, spec §9) run flag-independently: housekeeping never
egresses, so ship-inert still holds.

PHI/log rules: logs bind ids only — NEVER the endpoint URL (operator query
strings may carry tokens) and never ``str(exc)``; ``last_error`` records the
exception TYPE NAME only.
"""

import asyncio
import contextlib
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from usan_api import ssrf_guard, webhook_signing
from usan_api.db.models import WebhookEndpoint
from usan_api.db.session import get_session_factory
from usan_api.observability.custom_metrics import (
    WEBHOOK_DELIVERIES_TOTAL,
    WEBHOOK_ENDPOINTS_AUTO_DISABLED_TOTAL,
    WEBHOOK_PENDING_DELIVERIES,
)
from usan_api.repositories import webhook_endpoints as endpoints_repo
from usan_api.repositories import webhook_outbox
from usan_api.repositories.webhook_outbox import ClaimedDelivery
from usan_api.settings import Settings
from usan_api.ssrf_guard import SsrfBlocked


class WebhookDeliveryError(Exception):
    """Module-level error type (telnyx_messaging precedent). Deliberately NOT
    raised on the per-row path: ``last_error`` must carry the ORIGINAL
    exception's type name (spec §5.3), so ``deliver_one`` catches raw
    exception families instead of wrapping them.
    """


_HOUSEKEEPING_INTERVAL_S = 3600.0
_USER_AGENT = "usan-voice-engine-webhooks/1.0"

# Outcome strings (spec §5.3 label rule): the metric counts ONLY the first
# four; "skipped" is a breaker no-attempt, never a metric label.
_OUTCOME_DELIVERED = "delivered"
_OUTCOME_RETRY = "retry_scheduled"
_OUTCOME_FAILED = "failed"
_OUTCOME_SSRF = "ssrf_blocked"
_OUTCOME_SKIPPED = "skipped"
_COUNTED_OUTCOMES = frozenset({_OUTCOME_DELIVERED, _OUTCOME_RETRY, _OUTCOME_FAILED, _OUTCOME_SSRF})


def _record_outcome(event: str, outcome: str) -> None:
    """Increment-after-commit (house discipline): callers invoke this only
    after the row's outcome transaction commits. The label set is CLOSED
    (spec §9) — ``"skipped"`` falls through uncounted.
    """
    if outcome in _COUNTED_OUTCOMES:
        WEBHOOK_DELIVERIES_TOTAL.labels(event=event, outcome=outcome).inc()


def _housekeeping_due(last_run: float | None, now: float) -> bool:
    """Pure cadence seam: first cycle always runs; then hourly (spec §5.4)."""
    return last_run is None or now - last_run >= _HOUSEKEEPING_INTERVAL_S


def _build_client(settings: Settings) -> httpx.AsyncClient:
    """Client seam for tests. ``follow_redirects=False`` is load-bearing: a 3xx
    is a failure, never followed (redirect-to-internal SSRF classic, spec §8.2).
    """
    return httpx.AsyncClient(timeout=settings.webhook_delivery_timeout_s, follow_redirects=False)


async def deliver_one(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    claimed: ClaimedDelivery,
    client: httpx.AsyncClient,
) -> str:
    """Deliver one claimed row in its own short transaction (spec §5.3).

    Returns one of delivered|retry_scheduled|failed|ssrf_blocked|skipped.
    """
    log = logger.bind(
        component="webhook_delivery",
        delivery_id=str(claimed.id),
        endpoint_id=str(claimed.endpoint_id),
        event=claimed.event,
    )
    async with factory() as db:
        endpoint = await db.get(WebhookEndpoint, claimed.endpoint_id)
        if endpoint is None or not endpoint.enabled:
            # Breaker tripped mid-cycle (or endpoint deleted): skip without
            # POSTing. At attempts < 4 the already-bumped lease re-offers the
            # row if the operator re-enables (spec §5.3 step 1); a row skipped
            # on its TERMINAL rung (attempts=4) is unclaimable (the claim's
            # attempts < 4 predicate) and is settled to failed by the hourly
            # attempts=4 sweep (§5.4) — operator redeliver re-arms it.
            return _OUTCOME_SKIPPED
        url = endpoint.url
        secret = endpoint.secret

        response_code: int | None = None
        try:
            # Layer-2 SSRF gate before EVERY POST (spec §8.2).
            await ssrf_guard.resolve_public_or_raise(urlsplit(url).hostname or "")
            # Sign-what-you-send (spec §7): canonical bytes at send time, with
            # the dedupe key injected BEFORE signing (spec §6.1).
            body = dict(claimed.payload)
            body["delivery_id"] = str(claimed.id)
            raw = webhook_signing.canonical_bytes(body)
            ts_ms = int(time.time() * 1000)
            headers = {
                "Content-Type": "application/json",
                "User-Agent": _USER_AGENT,
                "X-Usan-Event": claimed.event,
                "X-Usan-Delivery-Id": str(claimed.id),
                "X-Usan-Signature": webhook_signing.signature_header(
                    ts_ms, webhook_signing.sign(secret, ts_ms, raw)
                ),
            }
            # Stream the response and drain at most max_response_bytes: a customer-
            # controlled receiver that returns an unbounded body could otherwise OOM the
            # API process (groups deliver concurrently). We only need the status code.
            async with client.stream("POST", url, content=raw, headers=headers) as response:
                response_code = response.status_code
                drained = 0
                async for chunk in response.aiter_bytes():
                    drained += len(chunk)
                    if drained >= settings.webhook_delivery_max_response_bytes:
                        break
                response.raise_for_status()
        except (httpx.HTTPError, OSError, ValueError, SsrfBlocked) as exc:
            # OSError covers socket.gaierror (NXDOMAIN — the most common
            # dead-receiver mode) propagating from resolve_public_or_raise;
            # httpx.TransportError does NOT subclass OSError, so both families
            # are needed (plan executor note 4). Only the TYPE NAME survives.
            terminal = claimed.attempts >= 4
            await webhook_outbox.mark_attempt_failed(
                db,
                claimed.id,
                response_code=response_code,
                last_error=type(exc).__name__,
                terminal=terminal,
            )
            failures = await endpoints_repo.increment_failures(db, claimed.endpoint_id)
            tripped = False
            if (
                failures is not None
                and failures >= settings.webhook_delivery_circuit_breaker_threshold
            ):
                tripped = await endpoints_repo.trip_breaker(db, claimed.endpoint_id)
            await db.commit()
            if tripped:
                # endpoint_id only — never the URL (spec §5.5/§8.3). Counter
                # after commit; trip_breaker's guarded UPDATE makes it one-shot.
                WEBHOOK_ENDPOINTS_AUTO_DISABLED_TOTAL.inc()
                logger.bind(
                    component="webhook_delivery", endpoint_id=str(claimed.endpoint_id)
                ).warning("Webhook endpoint auto-disabled by circuit breaker")
            if terminal:
                # Terminal attempt ALWAYS reads failed — including SSRF blocks —
                # so the failure alert cannot be muted (§5.3 alert honesty);
                # last_error keeps the diagnostic type name.
                outcome = _OUTCOME_FAILED
            elif isinstance(exc, SsrfBlocked):
                outcome = _OUTCOME_SSRF
            else:
                outcome = _OUTCOME_RETRY
            _record_outcome(claimed.event, outcome)
            log.bind(outcome=outcome, response_code=response_code).info(
                "Webhook delivery attempt settled"
            )
            return outcome

        await webhook_outbox.mark_delivered(db, claimed.id, response_code=response_code)
        await endpoints_repo.reset_failures(db, claimed.endpoint_id)
        await db.commit()
        _record_outcome(claimed.event, _OUTCOME_DELIVERED)
        log.bind(outcome=_OUTCOME_DELIVERED, response_code=response_code).info(
            "Webhook delivery attempt settled"
        )
        return _OUTCOME_DELIVERED


async def _deliver_group(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    rows: list[ClaimedDelivery],
    client: httpx.AsyncClient,
) -> dict[str, int]:
    """One endpoint's claimed rows, sequential oldest-first (spec §5.2)."""
    counts: dict[str, int] = {}
    for claimed in rows:
        outcome = await deliver_one(factory, settings, claimed, client)
        counts[outcome] = counts.get(outcome, 0) + 1
        if outcome == _OUTCOME_SKIPPED:
            # Breaker tripped mid-cycle: every later row in this group would
            # skip too — stop the group early (spec §5.2). Skipped rows stay
            # pending; below the terminal rung their bumped leases re-offer
            # them on re-enable, while attempts=4 skips settle to failed via
            # the hourly sweep (see deliver_one).
            break
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
        "swept": 0,
        "expired": 0,
        "pruned": 0,
    }

    # EVERY-CYCLE half (flag-INDEPENDENT): backlog depth, per-cycle not hourly
    # (spec §9) — the gauge is set here, NOT in the hourly housekeeping branch,
    # so flag-off backlogs and breaker-stranded rows stay observable.
    async with factory() as db:
        stats["pending"] = await webhook_outbox.count_pending(db)
    WEBHOOK_PENDING_DELIVERIES.set(stats["pending"])

    # HOURLY half (flag-INDEPENDENT): sweep/expire/prune in one txn (spec §5.4).
    if run_housekeeping:
        async with factory() as db:
            stats["swept"] = await webhook_outbox.sweep_crash_residue(db, now=now)
            stats["expired"] = await webhook_outbox.expire_stale_pending(db, now=now)
            stats["pruned"] = await webhook_outbox.prune_old(db, now=now)
            await db.commit()

    # Delivery half — the ONLY part the flag gates (spec §5.1).
    if not settings.webhook_delivery_enabled:
        return stats

    async with factory() as db:
        # Claim commits immediately — no locks across POSTs (spec §5.2).
        claimed = await webhook_outbox.claim_due(db, now=now)
        await db.commit()
    if not claimed:
        return stats

    groups: dict[uuid.UUID, list[ClaimedDelivery]] = {}
    for row in claimed:
        # claim_due returns oldest-due-first, so per-group order is preserved.
        groups.setdefault(row.endpoint_id, []).append(row)

    async with _build_client(settings) as client:
        results = await asyncio.gather(
            *(_deliver_group(factory, settings, rows, client) for rows in groups.values()),
            return_exceptions=True,
        )
    for endpoint_id, counts in zip(groups, results, strict=True):
        if isinstance(counts, BaseException):
            # A worker bug in one group (anything outside deliver_one's except
            # tuple) must not abort the cycle for every other endpoint. The
            # group's claimed rows keep their bumped leases and re-offer at the
            # next rung. Ids + type name only (spec §9).
            logger.bind(
                component="webhook_delivery",
                endpoint_id=str(endpoint_id),
                err=type(counts).__name__,
            ).error("Webhook delivery group failed")
            continue
        for outcome, count in counts.items():
            stats[outcome] += count
    return stats


async def run_poller(settings: Settings, stop: asyncio.Event) -> None:
    """Loop ``poll_once`` until ``stop`` is set — the schedule orchestrator's
    loop discipline (per-cycle exceptions are logged, never fatal; the interval
    sleep is a cancellable wait on ``stop``) with ONE deliberate deviation:
    the per-cycle ERROR logs ``type(exc).__name__`` only, never a traceback
    (spec §9 — exception text can embed endpoint URLs with tokens).
    """
    log = logger.bind(component="webhook_delivery")
    log.info(
        "Webhook delivery poller started (interval={i}s, delivery_enabled={e})",
        i=settings.webhook_delivery_poll_interval_s,
        e=settings.webhook_delivery_enabled,
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
            # "pending" is a depth, not work done — excluding it keeps a quiet
            # backlog (flag off / nothing due) from logging every cycle.
            if any(value for key, value in stats.items() if key != "pending"):
                log.info("Webhook delivery cycle work: {stats}", stats=stats)
        except Exception as exc:
            # Type name ONLY (spec §9) — never a traceback or str(exc), either
            # of which can embed the endpoint URL and its query-string tokens.
            log.bind(err=type(exc).__name__).error("Webhook delivery poll cycle failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.webhook_delivery_poll_interval_s)
    log.info("Webhook delivery poller stopped")
