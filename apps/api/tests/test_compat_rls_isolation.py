"""Foundational RLS-isolation test (T006): a compat session scoped to org A never sees
org B's rows — the compat key -> RLS tenant context is real cross-org isolation, not a
WHERE-clause convenience."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.compat import auth as compat_auth
from usan_api.db.session import _install_default_org_context


async def _seed_contact(
    super_async_url: str, org_id: uuid.UUID, name: str, phone: str
) -> uuid.UUID:
    # Seeded via the superuser engine, which bypasses RLS, so an explicit organization_id
    # is accepted (the app role could not insert cross-org under the WITH CHECK policy).
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            return (
                await conn.execute(
                    text(
                        "INSERT INTO contacts (organization_id, name, phone_e164, timezone) "
                        "VALUES (:org, :name, :phone, 'UTC') RETURNING id"
                    ),
                    {"org": org_id, "name": name, "phone": phone},
                )
            ).scalar_one()
    finally:
        await engine.dispose()


async def _delete_contacts(super_async_url: str, ids: list[uuid.UUID]) -> None:
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DELETE FROM contacts WHERE id = ANY(:ids)"), {"ids": ids})
    finally:
        await engine.dispose()


async def test_compat_session_isolated_to_org(
    two_orgs, async_database_url, app_async_database_url, app_role_password
):
    org_a, org_b = two_orgs
    phone_a = f"+1555{uuid.uuid4().int % 10_000_000:07d}"
    phone_b = f"+1556{uuid.uuid4().int % 10_000_000:07d}"
    cid_a = await _seed_contact(async_database_url, org_a, "A One", phone_a)
    cid_b = await _seed_contact(async_database_url, org_b, "B One", phone_b)

    engine = create_async_engine(app_async_database_url, poolclass=NullPool)
    _install_default_org_context(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async for session in compat_auth._scoped_session(factory, org_a):
            visible = {
                row[0] for row in (await session.execute(text("SELECT id FROM contacts"))).all()
            }
            break
        assert cid_a in visible, "org A's compat session must see org A's contact"
        assert cid_b not in visible, "org A's compat session must NOT see org B's contact"
    finally:
        await engine.dispose()
        # Remove seeded contacts so the two_orgs teardown can delete the orgs (FK RESTRICT).
        await _delete_contacts(async_database_url, [cid_a, cid_b])
