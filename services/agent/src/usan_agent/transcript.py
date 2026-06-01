"""Final-flush transcript persistence (design spec §9, v1).

At call end the JobContext shutdown callback reads the session's conversation
history, maps it to transcript segments (user/assistant messages + tool calls),
and POSTs them once (best-effort). Pure mapping in history_to_segments keeps it
unit-testable; the LiveKit history item shapes are duck-typed.
"""

import json
from datetime import UTC, datetime
from typing import Any

from usan_agent import api_client
from usan_agent.settings import Settings

_MESSAGE_ROLES = ("user", "assistant")


def _iso(created_at: float) -> str:
    return datetime.fromtimestamp(created_at, tz=UTC).isoformat()


def history_to_segments(items: list[Any]) -> list[dict[str, Any]]:
    """Map session.history.items to transcript-segment dicts.

    Keeps user/assistant messages with non-empty text and function calls (as
    role="tool" with parsed args); skips system/developer messages and
    function_call_output items.
    """
    segments: list[dict[str, Any]] = []
    for item in items:
        item_type = getattr(item, "type", None)
        if item_type == "message":
            role = getattr(item, "role", None)
            content = getattr(item, "text_content", None)
            if role not in _MESSAGE_ROLES or not content:
                continue
            segments.append({"role": role, "content": content, "started_at": _iso(item.created_at)})
        elif item_type == "function_call":
            try:
                args = json.loads(item.arguments)
            except (ValueError, TypeError):
                args = {}
            segments.append(
                {
                    "role": "tool",
                    "content": item.name,
                    "tool_name": item.name,
                    "tool_args": args if isinstance(args, dict) else {},
                    "started_at": _iso(item.created_at),
                }
            )
    return segments


def register_transcript_flush(ctx: Any, session: Any, call_id: str, settings: Settings) -> None:
    """Register a JobContext shutdown callback that flushes the transcript once."""

    async def _flush() -> None:
        segments = history_to_segments(session.history.items)
        if segments:
            await api_client.flush_transcript(call_id, settings, segments)

    ctx.add_shutdown_callback(_flush)
