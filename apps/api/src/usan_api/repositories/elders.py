import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import AgentProfile, Elder


async def create_elder(
    db: AsyncSession,
    *,
    name: str,
    phone_e164: str,
    timezone: str,
    external_id: str | None = None,
    preferred_voice: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Elder:
    elder = Elder(
        name=name,
        phone_e164=phone_e164,
        timezone=timezone,
        external_id=external_id,
        preferred_voice=preferred_voice,
        meta=metadata or {},
    )
    db.add(elder)
    await db.flush()
    await db.refresh(elder)
    return elder


async def get_elder(db: AsyncSession, elder_id: uuid.UUID) -> Elder | None:
    return await db.get(Elder, elder_id)


async def update_elder(
    db: AsyncSession, elder_id: uuid.UUID, fields: dict[str, Any]
) -> Elder | None:
    elder = await db.get(Elder, elder_id)
    if elder is None:
        return None
    for key, value in fields.items():
        setattr(elder, "meta" if key == "metadata" else key, value)
    await db.flush()
    await db.refresh(elder)
    return elder


async def get_elder_by_phone(db: AsyncSession, phone_e164: str) -> Elder | None:
    """Look up an elder by E.164 phone (UNIQUE) — the inbound caller-ID lookup."""
    result = await db.execute(select(Elder).where(Elder.phone_e164 == phone_e164))
    return result.scalar_one_or_none()


async def list_with_profile(
    db: AsyncSession, *, limit: int = 200, offset: int = 0
) -> list[tuple[Elder, str | None]]:
    """Elders with their assigned profile name (None if unassigned), ordered by name.

    Bounded by limit/offset: the elders table is the full patient roster (potentially
    thousands), so the admin list pages through it rather than selecting every row.
    """
    result = await db.execute(
        select(Elder, AgentProfile.name)
        .outerjoin(AgentProfile, Elder.agent_profile_id == AgentProfile.id)
        .order_by(Elder.name)
        .limit(limit)
        .offset(offset)
    )
    return [(row[0], row[1]) for row in result.all()]


async def assign_profile(
    db: AsyncSession, elder_id: uuid.UUID, profile_id: uuid.UUID | None
) -> Elder | None:
    """Set (or clear, with None) an elder's agent_profile_id. Caller commits."""
    elder = await db.get(Elder, elder_id)
    if elder is None:
        return None
    elder.agent_profile_id = profile_id
    await db.flush()
    return elder
