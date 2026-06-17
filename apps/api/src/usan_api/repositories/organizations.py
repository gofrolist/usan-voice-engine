import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import Organization


async def get_org_by_slug(db: AsyncSession, slug: str) -> Organization | None:
    result = await db.execute(select(Organization).where(Organization.slug == slug))
    return result.scalar_one_or_none()


async def get_org(db: AsyncSession, org_id: uuid.UUID) -> Organization | None:
    return await db.get(Organization, org_id)


async def create_org(db: AsyncSession, *, name: str, slug: str) -> Organization:
    org = Organization(name=name, slug=slug)
    db.add(org)
    await db.flush()
    return org


async def list_orgs(db: AsyncSession) -> list[Organization]:
    res = await db.execute(select(Organization).order_by(Organization.name))
    return list(res.scalars().all())
