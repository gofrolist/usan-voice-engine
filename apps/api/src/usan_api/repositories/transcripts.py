import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import Transcript

# Defensive upper bound on segments returned for a single call. A wellness call
# yields tens of segments; this caps a pathological or runaway transcript so the
# GET /v1/calls/{id} response (and the memory to build it) can't grow unbounded.
MAX_TRANSCRIPT_SEGMENTS = 1000


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


async def list_for_call(db: AsyncSession, call_id: uuid.UUID) -> list[Transcript]:
    """All transcript segments for a call, in conversation order (started_at, id)."""
    result = await db.execute(
        select(Transcript)
        .where(Transcript.call_id == call_id)
        .order_by(Transcript.started_at, Transcript.id)
        .limit(MAX_TRANSCRIPT_SEGMENTS)
    )
    return list(result.scalars().all())


async def delete_for_call(db: AsyncSession, call_id: uuid.UUID) -> None:
    """Delete all transcript rows for a call (used during soft-archive / PHI-redact)."""
    await db.execute(delete(Transcript).where(Transcript.call_id == call_id))


def _field(seg: Any, name: str) -> Any:
    return seg.get(name) if isinstance(seg, dict) else getattr(seg, name, None)
