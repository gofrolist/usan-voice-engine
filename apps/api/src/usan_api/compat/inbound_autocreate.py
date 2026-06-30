"""Unknown-recipient inbound SMS auto-create (Phase 4b-3).

When an inbound SMS arrives at a provisioned DID that carries an inbound_sms_agents
binding and matches no open sms_chat, auto-create an sms_chat oriented like an outbound
one (from=our DID, to=sender, ONGOING), persist the inbound turn role="sms" (the dedup
point), run one Vertex reply, persist it role="agent", and send it. Inert behind
telnyx_inbound_sms_autocreate_enabled. Single-org (RLS default org). PHI/secret-safe:
logs only message_id + type(exc).__name__ (never message text, reply, agent_id, or phone).
"""

from __future__ import annotations

from usan_api.db.models import PhoneNumber
from usan_api.observability.custom_metrics import WEBHOOKS_TOTAL


def _count(outcome: str) -> None:
    WEBHOOKS_TOTAL.labels(type="telnyx_sms", outcome=outcome).inc()


def _pick_inbound_sms_agent(pn: PhoneNumber | None) -> str | None:
    """First-entry agent_id token from the DID's inbound_sms_agents binding, else None.

    Deterministic first-entry mirrors _resolve_sms_agent's outbound[0] pick (chat_service.py)
    and is isolated here so a weighted-random pick can replace it without touching the caller.
    Our schema stores only the list (the oracle's inbound_sms_agent_id scalar is collapsed
    into it by the Phase 2 CRUD), so first-entry IS the scalar-equivalent.
    """
    agents = (pn.inbound_sms_agents if pn is not None else None) or []
    token = (agents[0] or {}).get("agent_id") if agents else None
    return token if isinstance(token, str) and token else None
