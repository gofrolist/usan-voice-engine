import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import Transcript


async def create_transcript_segments(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    segments: Sequence[Any],
) -> int:
    """Bulk-insert transcript segments for a call. Returns the number inserted.

    Each segment must expose: role, content, started_at, and optionally
    tool_name, tool_args, ended_at (Pydantic models or mappings both work via
    attribute/`getattr`).
    """
    rows = [
        Transcript(
            call_id=call_id,
            role=_field(seg, "role"),
            content=_field(seg, "content"),
            tool_name=_field(seg, "tool_name"),
            tool_args=_field(seg, "tool_args"),
            started_at=_field(seg, "started_at"),
            ended_at=_field(seg, "ended_at"),
        )
        for seg in segments
    ]
    db.add_all(rows)
    await db.flush()
    return len(rows)


def _field(seg: Any, name: str) -> Any:
    return seg.get(name) if isinstance(seg, dict) else getattr(seg, name, None)
