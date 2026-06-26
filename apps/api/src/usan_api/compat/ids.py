"""Reversible per-resource id codec between RetellAI external ids and native UUIDs.

No storage: each external id deterministically encodes a native ``uuid.UUID`` (and the
resource kind via a prefix), so create/get/list/webhook always present the SAME id for the
SAME row (SC-006). ``call_id`` is bare 32-char hex (RetellAI's shape); agents, response
engines (Retell-LLMs), and batches carry a kind prefix. Decoding validates the prefix and
raises ``CompatError(422)`` on a malformed id rather than 500-ing on a bad ``UUID(hex=...)``.
"""

from __future__ import annotations

import uuid

from usan_api.compat.errors import CompatError

_AGENT_PREFIX = "agent_"
_LLM_PREFIX = "llm_"
_BATCH_PREFIX = "batch_call_"


def encode_call_id(call_id: uuid.UUID) -> str:
    return call_id.hex


def decode_call_id(token: str) -> uuid.UUID:
    return _decode_hex(token, prefix="", kind="call_id")


def encode_agent_id(profile_id: uuid.UUID) -> str:
    return _AGENT_PREFIX + profile_id.hex


def decode_agent_id(token: str) -> uuid.UUID:
    return _decode_hex(token, prefix=_AGENT_PREFIX, kind="agent_id")


def encode_llm_id(profile_id: uuid.UUID) -> str:
    return _LLM_PREFIX + profile_id.hex


def decode_llm_id(token: str) -> uuid.UUID:
    return _decode_hex(token, prefix=_LLM_PREFIX, kind="llm_id")


def encode_batch_id(batch_id: uuid.UUID) -> str:
    return _BATCH_PREFIX + batch_id.hex


def decode_batch_id(token: str) -> uuid.UUID:
    return _decode_hex(token, prefix=_BATCH_PREFIX, kind="batch_call_id")


def encode_phone_number_cursor(pid: uuid.UUID) -> str:
    # Opaque cursor over the INTERNAL row id (bare hex) — never the E.164 (PHI).
    return pid.hex


def decode_phone_number_cursor(token: str) -> uuid.UUID:
    return _decode_hex(token, prefix="", kind="pagination_key")


def _decode_hex(token: str, *, prefix: str, kind: str) -> uuid.UUID:
    if not token.startswith(prefix):
        raise CompatError(422, f"invalid {kind}")
    try:
        return uuid.UUID(hex=token[len(prefix) :])
    except ValueError as exc:
        raise CompatError(422, f"invalid {kind}") from exc
