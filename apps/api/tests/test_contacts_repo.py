import uuid

from sqlalchemy import text

from usan_api.repositories import contacts as contacts_repo
from usan_api.tenant_context import set_tenant_context


async def test_delete_contact_removes_row(app_session):
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    c = await contacts_repo.create_contact(
        app_session, name="Del Me", phone_e164="+15550000010", timezone="America/Chicago"
    )
    assert await contacts_repo.delete_contact(app_session, c.id) is True
    assert await contacts_repo.get_contact(app_session, c.id) is None


async def test_delete_contact_missing_returns_false(app_session):
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    assert await contacts_repo.delete_contact(app_session, uuid.uuid4()) is False
