"""Custom Prometheus metrics (spec §8) and a tool-call tracking decorator.

IMPORTANT: prometheus_client appends "_total" to a Counter's exposed sample
name. To expose `usan_calls_total` the Counter is constructed as `usan_calls`.

Metrics register against the process-global default registry at import time, so
they are created exactly once per process (module import is cached). Labels are
a small, bounded, PHI-FREE set — never put call_id, contact id, phone number, or
free-text reasons in a label (unbounded cardinality and a PHI leak; spec §6/§15).

Increment-after-commit discipline (batch/scheduler spec §7): counters that
mirror a DB state transition are incremented only AFTER that transition's
commit, so a crash between write and commit can never double-count.

Structurally impossible usan_materialized_calls_total label combinations —
never emitted (batch/scheduler spec §7):
- result="skipped_contact_deleted" with source="schedule": deleting an contact
  CASCADE-deletes their schedules, so a schedule row never outlives its contact.
- result="rescheduled" with source="batch": batch targets have no next_run_at
  to go stale, hence nothing to reschedule.
result="skipped_window" with source="batch" IS emitted since the per-profile
policy unlock (small-unlocks spec §3.3.3 rule 2): a narrowed policy can empty
a batch dial window's intersection (policy ∩ window = ∅ → target skipped with
reason="window") — it is no longer structurally impossible.
"""

import functools
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from prometheus_client import Counter, Gauge

P = ParamSpec("P")
R = TypeVar("R")

# direction: inbound|outbound ; end_reason: the bounded call_status enum value
# (completed, voicemail_left, no_answer, busy, failed, dnc_blocked, cancelled).
CALLS_TOTAL = Counter(
    "usan_calls",
    "Calls reaching a terminal state at the agent end_call hook.",
    labelnames=("direction", "end_reason"),
)

# label `type`: the LiveKit event name (room_finished, egress_started, ...) or
# "unknown" when the signature failed before the event could be parsed.
# label `outcome`: ok|invalid.
WEBHOOKS_TOTAL = Counter(
    "usan_webhooks",
    "Inbound webhook deliveries by event type and verification outcome.",
    labelnames=("type", "outcome"),
)

# tool: the tool endpoint name ; outcome: ok|error.
TOOL_CALLS_TOTAL = Counter(
    "usan_tool_calls",
    "Tool endpoint invocations by tool and outcome.",
    labelnames=("tool", "outcome"),
)

# severity: routine|urgent ; category: the bounded FlagForFollowupRequest enum
# (medical, emotional, medication, safety, other). NEVER the free-text reason (PHI).
#
# Grafana alert (shipped as code in infra/grafana/provisioning/alerting/usan_alerts.yml,
# rule uid usan-urgent-followup-flag, spec §5.1):
#   EXPR   sum(increase(usan_followup_flags_total{severity="urgent"}[10m])) > 0
#   FOR    0m
#   on the `prometheus` datasource. The notification channel is an operator deploy
#   step (fill the usan-operator contact point); the rule itself is provisioned.
FOLLOWUP_FLAGS_TOTAL = Counter(
    "usan_followup_flags",
    "Follow-up flags created.",
    labelnames=("severity", "category"),
)

# No labels: a single global counter of callback requests recorded by schedule_callback.
# PHI-free by construction — requested_time_text / notes are NEVER label values (spec §9).
CALLBACK_REQUESTS_TOTAL = Counter(
    "usan_callback_requests",
    "Callback requests created.",
)

# status: sent|failed — the terminal outcome of a queued SMS row (incremented
# in flush_pending_sms AFTER the DB transition commits). PHI-free: no number/id.
SMS_MESSAGES_TOTAL = Counter(
    "usan_sms_messages",
    "SMS messages by terminal status.",
    labelnames=("status",),
)

# queue: follow_up_flag|callback_request ; to_status: acknowledged|resolved —
# bounded PHI-free labels (never the reason/notes text). Incremented only AFTER
# the transition's commit (house discipline above); idempotent same-status
# no-ops never increment.
ADMIN_QUEUE_TRANSITIONS_TOTAL = Counter(
    "usan_admin_queue_transitions",
    "Admin ops-queue status transitions.",
    labelnames=("queue", "to_status"),
)

# source: schedule|batch ; result: created|replayed|dnc_blocked|skipped_window|
# skipped_invalid_timezone|skipped_daily_cap|skipped_contact_deleted|rescheduled|
# key_conflict. Incremented per materialization decision, after commit. See the
# module docstring for the structurally impossible source x result combinations.
MATERIALIZED_CALLS_TOTAL = Counter(
    "usan_materialized_calls",
    "Schedule/batch materialization decisions.",
    labelnames=("source", "result"),
)

# event: created|started|completed|cancelled — incremented after each batch
# lifecycle transition commits.
BATCH_EVENTS_TOTAL = Counter(
    "usan_batch_events",
    "Batch lifecycle transitions.",
    labelnames=("event",),
)

# final_status: the bounded 7-value settled-chain outcome set (spec §6.2) —
# incremented by the finalizer, after commit.
BATCH_TARGETS_FINALIZED_TOTAL = Counter(
    "usan_batch_targets_finalized",
    "Batch targets reaching a settled chain outcome.",
    labelnames=("final_status",),
)

# reason: quiet_hours — the dial-time TCPA re-check re-queued a claimed dial
# instead of dialing it (incremented after the re-queue commit).
DIAL_REQUEUED_TOTAL = Counter(
    "usan_dial_requeued",
    "Claimed dials re-queued instead of dialed.",
    labelnames=("reason",),
)

# Set every retry-poller cycle (all flag states — pre-enable observability):
# the §5.4 recency-bounded count of dialing/ringing/in_progress calls.
IN_FLIGHT_CALLS = Gauge(
    "usan_in_flight_calls",
    "Recency-bounded dialing/ringing/in_progress calls (gate input).",
)

# Set every retry-poller cycle alongside IN_FLIGHT_CALLS.
DIAL_SLOTS_FREE = Gauge(
    "usan_dial_slots_free",
    "max_concurrent_calls - reserved - in_flight, floor 0. Alert: ==0 for 10m.",
)

# event: the 5 outbound webhook events + ping ; outcome: delivered|retry_scheduled|
# failed|ssrf_blocked — a CLOSED set (webhook spec §9): "skipped" (a breaker
# no-attempt) is an internal poller string, never recorded as a label. Terminal
# attempts ALWAYS read outcome="failed" — including SSRF blocks — so the
# delivery-failed alert cannot be muted (§5.3 alert honesty). Named
# usan_webhook_deliveries to avoid colliding with the INBOUND usan_webhooks
# counter above. Incremented only after each row's outcome commit.
#
# Grafana alert (alerts-as-code, rule uid usan-webhook-delivery-failed):
#   EXPR sum(increase(usan_webhook_deliveries_total{outcome="failed"}[30m])) > 0
WEBHOOK_DELIVERIES_TOTAL = Counter(
    "usan_webhook_deliveries",
    "Outbound webhook delivery attempts by event and outcome.",
    labelnames=("event", "outcome"),
)

# No labels; exactly one increment per breaker trip (the guarded-UPDATE one-shot,
# webhook spec §5.5), after its commit. A tripped breaker stops the endpoint's
# rows from ever reaching outcome="failed" — silently muting the failure alert —
# so the trip itself must page (rule uid usan-webhook-endpoint-auto-disabled):
#   EXPR sum(increase(usan_webhook_endpoints_auto_disabled_total[30m])) > 0
WEBHOOK_ENDPOINTS_AUTO_DISABLED_TOTAL = Counter(
    "usan_webhook_endpoints_auto_disabled",
    "Webhook endpoints auto-disabled by the circuit breaker.",
)

# Set EVERY webhook-poller cycle (not hourly), flag-independently — pre-enable
# observability of flag-off backlogs and breaker-stranded rows (webhook spec
# §9/§5.1). No labels: per-endpoint labels would be unbounded over time; the
# per-endpoint pending count surfaces in GET /v1/webhook-endpoints instead.
WEBHOOK_PENDING_DELIVERIES = Gauge(
    "usan_webhook_pending_deliveries",
    "Outbox rows with status='pending' (backlog visibility; set every poller cycle).",
)


def track_tool(tool: str) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorate a tool route handler to record usan_tool_calls_total{tool,outcome}.

    Wraps the handler body (not its dependencies): a request rejected by an auth
    dependency never reaches here, so only invocations that enter the handler are
    counted. functools.wraps preserves __wrapped__, so FastAPI still resolves the
    handler's real signature (Depends/Body params) through the wrapper.
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                result = await func(*args, **kwargs)
            except Exception:
                TOOL_CALLS_TOTAL.labels(tool=tool, outcome="error").inc()
                raise
            TOOL_CALLS_TOTAL.labels(tool=tool, outcome="ok").inc()
            return result

        return wrapper

    return decorator
