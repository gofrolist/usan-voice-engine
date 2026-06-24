"""Task 9: cross-org RLS isolation proof for the new admin orchestration surface.

Proves that the contacts, DNC (and by extension schedules/calls) endpoints added
in the admin orchestration PR leak nothing across tenants:

* Contacts seeded into org A are invisible to org B and vice-versa.
* A direct fetch of org B's contact id returns 404 when acting as org A (not 200,
  not 500 — RLS hides the row entirely).
* A DNC entry added by org A is absent from org B's DNC list.

Uses the ``isolation_client`` fixture and ``_act_as_cookie`` helper from
``test_rls_p2_isolation.py`` verbatim — the same per-principal ``get_tenant_db``
override that mirrors production RLS scoping.  Seeds rows via the superuser engine
with an explicit ``organization_id`` so RLS is bypassed during setup (like
``_seed_profile_in_org`` in the P2 isolation suite).
"""

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

# Re-use the isolation_client fixture and act-as cookie helper from the P2 suite.
# isolation_client is a pytest fixture defined in test_rls_p2_isolation; importing
# it here makes pytest discover it in this module's scope without re-implementing
# the per-principal get_tenant_db override.
from tests.test_rls_p2_isolation import _act_as_cookie, isolation_client  # noqa: F401,F811

# ---------------------------------------------------------------------------
# Superuser-engine seeding helpers
# ---------------------------------------------------------------------------


def _super_url(app_async_database_url: str) -> str:
    """Derive the superuser (RLS-bypassing) async DSN from the usan_app one."""
    return app_async_database_url.replace("usan_app:usan_app@", "usan:usan@", 1)


async def _seed_contact_in_org(super_async_url: str, org_id: uuid.UUID, phone: str) -> str:
    """Insert a contact directly into ``org_id`` (bypassing RLS) and return its id.

    organization_id is set explicitly so the row lands in the target org regardless
    of the session-level app.current_org setting (mirrors _seed_profile_in_org).
    """
    cid = str(uuid.uuid4())
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO contacts (id, organization_id, name, phone_e164, timezone) "
                    "VALUES (CAST(:id AS uuid), :org, :n, :p, 'America/New_York')"
                ),
                {"id": cid, "org": org_id, "n": f"Contact {phone}", "p": phone},
            )
    finally:
        await engine.dispose()
    return cid


async def _seed_dnc_in_org(super_async_url: str, org_id: uuid.UUID, phone: str) -> None:
    """Insert a DNC entry directly into ``org_id`` (bypassing RLS)."""
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO dnc_list (phone_e164, organization_id, reason) "
                    "VALUES (:p, :org, 'rls-test') "
                    "ON CONFLICT DO NOTHING"
                ),
                {"p": phone, "org": org_id},
            )
    finally:
        await engine.dispose()


async def _seed_super_admin_local(super_async_url: str, email: str) -> None:
    """Ensure a super-admin row exists (idempotent)."""
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, is_super_admin, status, added_by) "
                    "VALUES (:e, true, 'active', 'test') "
                    "ON CONFLICT (email) DO UPDATE SET is_super_admin = true"
                ),
                {"e": email.lower()},
            )
    finally:
        await engine.dispose()


async def _delete_contacts_in_orgs(super_async_url: str, *org_ids: uuid.UUID) -> None:
    """Remove contacts (and cascade-dependent schedules) in the given orgs before teardown.

    Contacts FK the org; they must go before ``two_orgs`` deletes the organizations.
    call_schedules FK contacts (ON DELETE CASCADE), so deleting contacts is sufficient.
    """
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM contacts WHERE organization_id = ANY(:orgs)"),
                {"orgs": list(org_ids)},
            )
    finally:
        await engine.dispose()


async def _delete_dnc_in_orgs(super_async_url: str, *org_ids: uuid.UUID) -> None:
    """Remove DNC entries in the given orgs before teardown."""
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM dnc_list WHERE organization_id = ANY(:orgs)"),
                {"orgs": list(org_ids)},
            )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# 1. contacts list — org A sees only its own contact
# ---------------------------------------------------------------------------


def test_admin_contacts_isolated_between_orgs(isolation_client, two_orgs):  # noqa: F811
    """GET /v1/admin/contacts scoped to org A returns A's contact and hides B's."""
    client, super_url = isolation_client
    org_a, org_b = two_orgs

    ca = asyncio.run(_seed_contact_in_org(super_url, org_a, "+15551260001"))
    cb = asyncio.run(_seed_contact_in_org(super_url, org_b, "+15551260002"))
    asyncio.run(_seed_super_admin_local(super_url, "staff-rls@usan.com"))
    try:
        # Acting as org A: A's contact is visible, B's is not.
        ids_a = {
            c["id"]
            for c in client.get(
                "/v1/admin/contacts",
                cookies=_act_as_cookie("staff-rls@usan.com", org_a),
            ).json()
        }
        assert ca in ids_a, "org A's contact must appear in org A's listing"
        assert cb not in ids_a, "org B's contact must be absent from org A's listing"

        # Acting as org B: B's contact is visible, A's is not.
        ids_b = {
            c["id"]
            for c in client.get(
                "/v1/admin/contacts",
                cookies=_act_as_cookie("staff-rls@usan.com", org_b),
            ).json()
        }
        assert cb in ids_b, "org B's contact must appear in org B's listing"
        assert ca not in ids_b, "org A's contact must be absent from org B's listing"
    finally:
        asyncio.run(_delete_contacts_in_orgs(super_url, org_a, org_b))


# ---------------------------------------------------------------------------
# 2. contact detail — cross-org fetch returns 404, not 200
# ---------------------------------------------------------------------------


def test_admin_contact_detail_cross_org_returns_404(isolation_client, two_orgs):  # noqa: F811
    """GET /v1/admin/contacts/{id} acting as org A returns 404 for org B's contact id."""
    client, super_url = isolation_client
    org_a, org_b = two_orgs

    asyncio.run(_seed_contact_in_org(super_url, org_a, "+15551260003"))
    cb = asyncio.run(_seed_contact_in_org(super_url, org_b, "+15551260004"))
    asyncio.run(_seed_super_admin_local(super_url, "staff-rls@usan.com"))
    try:
        r = client.get(
            f"/v1/admin/contacts/{cb}",
            cookies=_act_as_cookie("staff-rls@usan.com", org_a),
        )
        assert r.status_code == 404, (
            f"expected 404 (RLS hides cross-org row), got {r.status_code}: {r.text}"
        )
    finally:
        asyncio.run(_delete_contacts_in_orgs(super_url, org_a, org_b))


# ---------------------------------------------------------------------------
# 3. DNC list — org A's entry is absent from org B's view
# ---------------------------------------------------------------------------


def test_admin_dnc_isolated_between_orgs(isolation_client, two_orgs):  # noqa: F811
    """A DNC entry added under org A is invisible when listing as org B."""
    client, super_url = isolation_client
    org_a, org_b = two_orgs

    phone_a = "+15551260005"
    asyncio.run(_seed_dnc_in_org(super_url, org_a, phone_a))
    asyncio.run(_seed_super_admin_local(super_url, "staff-rls@usan.com"))
    try:
        # Org B must not see org A's DNC entry.
        listed_b = client.get(
            "/v1/admin/dnc",
            cookies=_act_as_cookie("staff-rls@usan.com", org_b),
        ).json()
        masked_phones_b = {row["masked_phone"] for row in listed_b}
        # The masked form ends with the last 4 digits.
        assert not any(m.endswith("0005") for m in masked_phones_b), (
            "org A's DNC entry must be invisible to org B"
        )

        # Org A does see its own entry.
        listed_a = client.get(
            "/v1/admin/dnc",
            cookies=_act_as_cookie("staff-rls@usan.com", org_a),
        ).json()
        assert any(row["masked_phone"].endswith("0005") for row in listed_a), (
            "org A's DNC entry must be visible to org A"
        )
    finally:
        asyncio.run(_delete_dnc_in_orgs(super_url, org_a, org_b))
