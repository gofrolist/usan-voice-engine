"""Migration 0040: phone_numbers is TenantScoped + FORCE-RLS, and usan_app can CRUD it.

Mirrors the existing compat RLS-isolation test pattern: connect as the non-superuser
usan_app role (app_session), set the tenant context, and assert a row written under org A
is invisible under org B (RLS) and that usan_app has the table grant (no permission error).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from usan_api.db.models import PhoneNumber
from usan_api.tenant_context import set_tenant_context


@pytest.mark.asyncio
async def test_phone_numbers_rls_isolation_and_grant(two_orgs, app_session) -> None:
    org_a, org_b = two_orgs

    await set_tenant_context(app_session, org_a)
    app_session.add(PhoneNumber(phone_e164="+15550001111", phone_number_type="custom"))
    await app_session.flush()  # usan_app INSERT must succeed (GRANT present)
    rows_a = (await app_session.execute(select(PhoneNumber))).scalars().all()
    assert [r.phone_e164 for r in rows_a] == ["+15550001111"]

    # Same connection, switch tenant context: RLS hides org A's row from org B.
    await set_tenant_context(app_session, org_b)
    rows_b = (await app_session.execute(select(PhoneNumber))).scalars().all()
    assert rows_b == []
