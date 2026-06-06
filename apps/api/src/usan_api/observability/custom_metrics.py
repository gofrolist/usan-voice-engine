"""Custom Prometheus metrics (spec §8) and a tool-call tracking decorator.

IMPORTANT: prometheus_client appends "_total" to a Counter's exposed sample
name. To expose `usan_calls_total` the Counter is constructed as `usan_calls`.

Metrics register against the process-global default registry at import time, so
they are created exactly once per process (module import is cached). Labels are
a small, bounded, PHI-FREE set — never put call_id, elder id, phone number, or
free-text reasons in a label (unbounded cardinality and a PHI leak; spec §6/§15).
"""

import functools
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

from prometheus_client import Counter

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
