"""Reversible per-resource id codec between RetellAI external ids and native UUIDs.

No storage: each external id deterministically encodes a native ``uuid.UUID`` (and the
resource kind via a prefix), so create/get/list/webhook always present the SAME id for the
SAME row (SC-006). ``call_id`` is bare 32-char hex (RetellAI's shape); agents, response
engines (Retell-LLMs), and batches carry a kind prefix. Decoding validates the prefix and
raises ``CompatError(422)`` on a malformed id rather than 500-ing on a bad ``UUID(hex=...)``.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime

from usan_api.compat.errors import CompatError

_AGENT_PREFIX = "agent_"
_LLM_PREFIX = "llm_"
_BATCH_PREFIX = "batch_call_"
_CHAT_PREFIX = "chat_"
_MESSAGE_PREFIX = "message_"
_KB_PREFIX = "knowledge_base_"
_KB_SOURCE_PREFIX = "source_"
_CONVERSATION_FLOW_PREFIX = "conversation_flow_"


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


def encode_chat_id(chat_id: uuid.UUID) -> str:
    return _CHAT_PREFIX + chat_id.hex


def decode_chat_id(token: str) -> uuid.UUID:
    return _decode_hex(token, prefix=_CHAT_PREFIX, kind="chat_id")


def encode_kb_id(kb_id: uuid.UUID) -> str:
    return _KB_PREFIX + kb_id.hex


def decode_kb_id(token: str) -> uuid.UUID:
    return _decode_hex(token, prefix=_KB_PREFIX, kind="knowledge_base_id")


def encode_kb_source_id(source_id: uuid.UUID) -> str:
    return _KB_SOURCE_PREFIX + source_id.hex


def decode_kb_source_id(token: str) -> uuid.UUID:
    return _decode_hex(token, prefix=_KB_SOURCE_PREFIX, kind="source_id")


def encode_message_id(message_id: uuid.UUID) -> str:
    return _MESSAGE_PREFIX + message_id.hex


def encode_conversation_flow_id(flow_id: uuid.UUID) -> str:
    return _CONVERSATION_FLOW_PREFIX + flow_id.hex


def decode_conversation_flow_id(token: str) -> uuid.UUID:
    return _decode_hex(token, prefix=_CONVERSATION_FLOW_PREFIX, kind="conversation_flow_id")


def encode_conversation_flow_cursor(created_at: datetime, fid: uuid.UUID) -> str:
    """Opaque (created_at, id) keyset cursor (mirror of encode_phone_number_cursor)."""
    raw = f"{created_at.isoformat()}|{fid.hex}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_conversation_flow_cursor(token: str) -> tuple[datetime, uuid.UUID]:
    """Decode a cursor token back to (created_at, id). Raises CompatError(422) on any bad input."""
    try:
        padding = 4 - len(token) % 4
        padded = token + "=" * (padding % 4)
        raw = base64.urlsafe_b64decode(padded).decode()
        ts_part, hex_part = raw.split("|", 1)
        created_at = datetime.fromisoformat(ts_part)
        fid = uuid.UUID(hex=hex_part)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise CompatError(422, "invalid pagination_key") from exc
    return created_at, fid


def encode_phone_number_cursor(created_at: datetime, pid: uuid.UUID) -> str:
    """Opaque self-contained cursor encoding (created_at, id) — no row lookup needed on decode."""
    raw = f"{created_at.isoformat()}|{pid.hex}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_phone_number_cursor(token: str) -> tuple[datetime, uuid.UUID]:
    """Decode a cursor token back to (created_at, id). Raises CompatError(422) on any bad input."""
    try:
        # Re-pad to a multiple of 4 before decoding.
        padding = 4 - len(token) % 4
        padded = token + "=" * (padding % 4)
        raw = base64.urlsafe_b64decode(padded).decode()
        ts_part, hex_part = raw.split("|", 1)
        created_at = datetime.fromisoformat(ts_part)
        pid = uuid.UUID(hex=hex_part)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise CompatError(422, "invalid pagination_key") from exc
    return created_at, pid


def _decode_hex(token: str, *, prefix: str, kind: str) -> uuid.UUID:
    if not token.startswith(prefix):
        raise CompatError(422, f"invalid {kind}")
    try:
        return uuid.UUID(hex=token[len(prefix) :])
    except ValueError as exc:
        raise CompatError(422, f"invalid {kind}") from exc
