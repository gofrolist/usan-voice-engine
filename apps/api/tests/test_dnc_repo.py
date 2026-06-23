import uuid

from sqlalchemy import text

from usan_api.repositories import dnc as dnc_repo
from usan_api.tenant_context import set_tenant_context


async def _org(app_session) -> uuid.UUID:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    return org_id


async def test_list_entries_returns_added(app_session):
    await _org(app_session)
    await dnc_repo.add_entry(app_session, "+15550000001", "requested")
    await dnc_repo.add_entry(app_session, "+15550000002", None)
    rows = await dnc_repo.list_entries(app_session, limit=50, offset=0)
    phones = {r.phone_e164 for r in rows}
    assert {"+15550000001", "+15550000002"} <= phones


async def test_list_entries_respects_limit(app_session):
    await _org(app_session)
    await dnc_repo.add_entry(app_session, "+15550000003", None)
    await dnc_repo.add_entry(app_session, "+15550000004", None)
    rows = await dnc_repo.list_entries(app_session, limit=1, offset=0)
    assert len(rows) == 1
