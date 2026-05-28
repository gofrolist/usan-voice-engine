import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import Elder


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
