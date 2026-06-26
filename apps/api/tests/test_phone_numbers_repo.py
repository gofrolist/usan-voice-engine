"""phone_numbers repository: CRUD + keyset list under a single org context (usan_app)."""

from __future__ import annotations

import pytest

from usan_api.db.models import PhoneNumber
from usan_api.repositories import phone_numbers as repo
from usan_api.tenant_context import set_tenant_context


@pytest.mark.asyncio
async def test_crud_and_keyset_list(two_orgs, app_session) -> None:
    org_a, _ = two_orgs
    await set_tenant_context(app_session, org_a)

    a = await repo.create_phone_number(
        app_session, phone_e164="+15550000001", phone_number_type="custom", nickname="one"
    )
    await repo.create_phone_number(
        app_session, phone_e164="+15550000002", phone_number_type="custom"
    )
    assert isinstance(a, PhoneNumber)

    got = await repo.get_by_e164(app_session, "+15550000001")
    assert got is not None
    assert got.nickname == "one"
    assert await repo.get_by_e164(app_session, "+19999999999") is None

    updated = await repo.update_by_e164(app_session, "+15550000001", {"nickname": "renamed"})
    assert updated is not None
    assert updated.nickname == "renamed"

    page = await repo.list_phone_numbers(app_session, limit=10, descending=True, after=None)
    assert {p.phone_e164 for p in page} == {"+15550000001", "+15550000002"}

    # keyset: page after the newest row excludes it (cursor carries created_at + id)
    newest = page[0]
    after = await repo.list_phone_numbers(
        app_session, limit=10, descending=True, after=(newest.created_at, newest.id)
    )
    assert newest.id not in {p.id for p in after}

    assert await repo.delete_by_e164(app_session, "+15550000002") is True
    assert await repo.delete_by_e164(app_session, "+15550000002") is False
