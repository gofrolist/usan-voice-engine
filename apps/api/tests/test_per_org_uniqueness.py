"""Per-org composite uniqueness (P2 plan Unit D, Task D1 — migration 0034).

The P1-era single-column uniques on tenant-scoped natural keys (e.g.
``contacts.phone_e164``) become composite ``UNIQUE(column, organization_id)``,
so two different orgs may legitimately hold the *same* natural key while a
duplicate *within* one org is still rejected.

Runs as the non-superuser ``usan_app`` role (``app_session``) under RLS, so each
insert must run with the matching tenant context (RLS WITH CHECK rejects an
``organization_id`` that differs from ``app.current_org``). Asserts via the
asyncpg engine the rest of the RLS suite uses — this env has no sync Postgres
driver, so a sync ``sa.create_engine(postgresql://...)`` would raise
ModuleNotFoundError rather than exercise the schema.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from usan_api.tenant_context import set_tenant_context

_INSERT_CONTACT = text(
    "INSERT INTO contacts (id, name, phone_e164, timezone, organization_id) "
    "VALUES (gen_random_uuid(), 'C', :p, 'America/New_York', CAST(:o AS uuid))"
)


async def _insert_contact(session, *, phone: str, org_id: uuid.UUID) -> None:
    await set_tenant_context(session, org_id)
    await session.execute(_INSERT_CONTACT, {"p": phone, "o": str(org_id)})
    await session.flush()


async def test_same_phone_allowed_in_two_orgs(two_orgs, app_session):
    """The same natural key in two different orgs no longer collides."""
    org_a, org_b = two_orgs
    phone = f"+1{uuid.uuid4().int % 9_000_000_000 + 1_000_000_000}"
    await _insert_contact(app_session, phone=phone, org_id=org_a)
    await _insert_contact(app_session, phone=phone, org_id=org_b)
    # Both inserts committed cleanly under their own org context.
    await set_tenant_context(app_session, org_a)
    count_a = (
        await app_session.execute(
            text("SELECT count(*) FROM contacts WHERE phone_e164 = :p"), {"p": phone}
        )
    ).scalar_one()
    assert count_a == 1  # RLS shows only org A's row


async def test_duplicate_phone_within_one_org_rejected(two_orgs, app_session):
    """A duplicate natural key *within* a single org still violates uniqueness."""
    org_a, _ = two_orgs
    phone = f"+1{uuid.uuid4().int % 9_000_000_000 + 1_000_000_000}"
    await _insert_contact(app_session, phone=phone, org_id=org_a)
    with pytest.raises(IntegrityError):
        await _insert_contact(app_session, phone=phone, org_id=org_a)
