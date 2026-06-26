"""Shared compat serialization helpers: RetellAI timestamps are Unix epoch ms (FR-051)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any


def to_ms(dt: datetime | None) -> int | None:
    """A datetime -> Unix epoch milliseconds, or None passthrough.

    RetellAI emits every timestamp as integer epoch ms. The DB stores timezone-aware UTC;
    a naive datetime is treated as UTC (defensive) rather than the host's local zone.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def duration_ms(seconds: int | None) -> int | None:
    """Native duration in whole seconds -> milliseconds, or None passthrough."""
    if seconds is None:
        return None
    return seconds * 1000


# RetellAI `metadata` (CRM-only) is stored as ONE JSON blob under this reserved key inside the
# native `dynamic_vars` JSONB, so it persists + round-trips on get/update WITH FULL TYPE
# FIDELITY (ints, bools, nested objects survive a create -> get round-trip) while staying a
# flat string VALUE the native validator accepts. The other (bare) keys are the agent-visible
# retell_llm_dynamic_variables. CRM dynamic-var keys starting with this prefix are rejected at
# create (call_create) so they can never collide with the reserved namespace.
RESERVED_VAR_PREFIX = "__meta"
_META_KEY = "__meta__"
_UNHONORED_KEY = "__meta_unhonored__"


def pack_unhonored(
    packed: dict[str, Any],
    *,
    agent_override: dict[str, Any] | None,
    current_node_id: str | None,
    current_state: str | None,
) -> dict[str, Any]:
    """Stash accepted-but-not-honored request fields under a reserved key that
    ``unpack_dynamic_vars`` strips, so they persist for audit WITHOUT polluting the
    echoed ``metadata`` / ``retell_llm_dynamic_variables``. Mirrors the ``__meta__``
    mechanism; shares the ``__meta`` prefix that client keys are already barred from."""
    extras = {
        key: value
        for key, value in (
            ("agent_override", agent_override),
            ("current_node_id", current_node_id),
            ("current_state", current_state),
        )
        if value is not None
    }
    if not extras:
        return packed
    return {**packed, _UNHONORED_KEY: json.dumps(extras)}


def pack_dynamic_vars(
    dynamic_variables: dict[str, Any] | None, metadata: dict[str, Any] | None
) -> dict[str, Any]:
    """Merge the CRM's retell_llm_dynamic_variables (bare) + metadata (one JSON blob under the
    reserved ``__meta__`` key) into one flat dict the native ``dynamic_vars`` column accepts."""
    packed: dict[str, Any] = {str(k): v for k, v in (dynamic_variables or {}).items()}
    if metadata:
        packed[_META_KEY] = json.dumps(metadata)
    return packed


def unpack_dynamic_vars(
    stored: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Inverse of ``pack_dynamic_vars``: split a stored ``dynamic_vars`` dict back into
    ``(retell_llm_dynamic_variables, metadata)`` with original metadata types preserved.
    The reserved un-honored audit blob is dropped (never echoed)."""
    rest = dict(stored or {})
    raw = rest.pop(_META_KEY, None)
    rest.pop(_UNHONORED_KEY, None)
    metadata: dict[str, Any] = json.loads(raw) if isinstance(raw, str) and raw else {}
    return rest, metadata
