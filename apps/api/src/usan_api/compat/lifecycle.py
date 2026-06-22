"""Compat call-lifecycle webhook emission (feature 003 / US2, T030).

The ONE sanctioned native->compat seam (anticipated by T030): native call-status
transitions live in ``repositories.calls``, so compat webhook emission must hook there. The
native ``_enqueue_call_started`` / ``_enqueue_call_completed`` helpers (and summarization's
summary-created path) call ``enqueue_compat_call_event`` AFTER the native enqueue, inside
the SAME guarded transaction — so a rolled-back transition emits nothing.

For a call whose resolved agent has NO compat subscription (every native-only call), this is
a single indexed lookup that returns None and enqueues nothing — behavior-preserving for the
native plane (SC-007). Import is LOCAL at each native call site to keep ``repositories.calls``
free of an import-time dependency on the compat package.
"""

from __future__ import annotations

import uuid
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import CallDirection
from usan_api.db.models import Call
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import compat_webhooks as compat_webhooks_repo

# Native transition -> Retell event name (the {event} field of the {event, call} body).
CALL_STARTED = "call_started"
CALL_ENDED = "call_ended"
CALL_ANALYZED = "call_analyzed"


async def _resolve_agent_profile_id(db: AsyncSession, call: Call) -> uuid.UUID | None:
    """The agent profile that handled the call: the override if any, else the direction
    default (mirrors compat.call_serializer._resolve_agent). None when none resolves."""
    direction: Literal["inbound", "outbound"] = (
        "inbound" if call.direction is CallDirection.INBOUND else "outbound"
    )
    profile = None
    if call.profile_override is not None:
        profile = await agent_profiles_repo.get_profile(db, call.profile_override)
    if profile is None:
        profile = await agent_profiles_repo.get_default_profile(db, direction)
    return profile.id if profile is not None else None


async def enqueue_compat_call_event(db: AsyncSession, call: Call, *, event: str) -> None:
    """Enqueue a compat webhook delivery for ``call`` IF its agent has a subscription to
    ``event`` (RLS-scoped to the call's org). A no-op otherwise — never raises into the
    caller's transaction on a benign miss."""
    agent_profile_id = await _resolve_agent_profile_id(db, call)
    if agent_profile_id is None:
        return
    endpoint = await compat_webhooks_repo.get_subscription_for_agent(
        db, agent_profile_id=agent_profile_id, event=event
    )
    if endpoint is None:
        return
    await compat_webhooks_repo.enqueue_call_event(
        db, endpoint_id=endpoint.id, event=event, call_id=call.id
    )
