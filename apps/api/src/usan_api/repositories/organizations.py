import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import Organization


async def get_org_by_slug(db: AsyncSession, slug: str) -> Organization | None:
    result = await db.execute(select(Organization).where(Organization.slug == slug))
    return result.scalar_one_or_none()


async def get_org(db: AsyncSession, org_id: uuid.UUID) -> Organization | None:
    return await db.get(Organization, org_id)


async def get_orgs_by_ids(
    db: AsyncSession, org_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, Organization]:
    """Batch-load organizations by id in a single query, keyed by id.

    Avoids the N+1 round-trip of calling ``get_org`` once per membership.
    """
    ids = list(dict.fromkeys(org_ids))  # de-dupe, preserve order
    if not ids:
        return {}
    res = await db.execute(select(Organization).where(Organization.id.in_(ids)))
    return {o.id: o for o in res.scalars().all()}


async def create_org(db: AsyncSession, *, name: str, slug: str) -> Organization:
    org = Organization(name=name, slug=slug)
    db.add(org)
    await db.flush()
    return org


async def list_orgs(db: AsyncSession) -> list[Organization]:
    res = await db.execute(select(Organization).order_by(Organization.name))
    return list(res.scalars().all())
