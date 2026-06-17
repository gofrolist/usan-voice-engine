import pytest

from usan_api.db.base import AdminRole
from usan_api.repositories import memberships as repo

# Fixtures `two_orgs`, `app_session` defined in conftest (Task A3 additions).
# memberships + admin_users are GLOBAL (non-RLS) tables, so these repo functions
# are scoped by organization_id in app code; the app_session runs as usan_app.


async def test_add_and_list_members(two_orgs, app_session):
    org_a, _ = two_orgs
    await repo.add_member(
        app_session, email="a@x.com", org_id=org_a, role=AdminRole.ADMIN, added_by="t"
    )
    members = await repo.list_members(app_session, org_a)
    assert [m.email for m in members] == ["a@x.com"]


async def test_add_member_normalizes_and_upserts_role(two_orgs, app_session):
    org_a, _ = two_orgs
    await repo.add_member(
        app_session, email="A@X.com", org_id=org_a, role=AdminRole.VIEWER, added_by="t"
    )
    m = await repo.add_member(
        app_session, email="a@x.com", org_id=org_a, role=AdminRole.ADMIN, added_by="t2"
    )
    assert m.email == "a@x.com"
    assert m.role is AdminRole.ADMIN
    members = await repo.list_members(app_session, org_a)
    assert len(members) == 1  # upsert, not a second row


async def test_get_membership_and_scoping(two_orgs, app_session):
    org_a, org_b = two_orgs
    await repo.add_member(
        app_session, email="a@x.com", org_id=org_a, role=AdminRole.ADMIN, added_by="t"
    )
    assert await repo.get_membership(app_session, "a@x.com", org_a) is not None
    # Same person is NOT a member of org B.
    assert await repo.get_membership(app_session, "a@x.com", org_b) is None
    assert await repo.list_members(app_session, org_b) == []


async def test_list_memberships_for_email_across_orgs(two_orgs, app_session):
    org_a, org_b = two_orgs
    await repo.add_member(
        app_session, email="a@x.com", org_id=org_a, role=AdminRole.ADMIN, added_by="t"
    )
    await repo.add_member(
        app_session, email="a@x.com", org_id=org_b, role=AdminRole.VIEWER, added_by="t"
    )
    orgs = {
        m.organization_id for m in await repo.list_memberships_for_email(app_session, "a@x.com")
    }
    assert orgs == {org_a, org_b}


async def test_count_org_admins(two_orgs, app_session):
    org_a, _ = two_orgs
    await repo.add_member(
        app_session, email="a@x.com", org_id=org_a, role=AdminRole.ADMIN, added_by="t"
    )
    await repo.add_member(
        app_session, email="b@x.com", org_id=org_a, role=AdminRole.VIEWER, added_by="t"
    )
    assert await repo.count_org_admins(app_session, org_a) == 1


async def test_set_member_role(two_orgs, app_session):
    org_a, _ = two_orgs
    await repo.add_member(
        app_session, email="a@x.com", org_id=org_a, role=AdminRole.ADMIN, added_by="t"
    )
    await repo.add_member(
        app_session, email="b@x.com", org_id=org_a, role=AdminRole.VIEWER, added_by="t"
    )
    m = await repo.set_member_role(app_session, email="b@x.com", org_id=org_a, role=AdminRole.ADMIN)
    assert m.role is AdminRole.ADMIN


async def test_set_member_role_missing_raises_keyerror(two_orgs, app_session):
    org_a, _ = two_orgs
    with pytest.raises(KeyError):
        await repo.set_member_role(
            app_session, email="nobody@x.com", org_id=org_a, role=AdminRole.ADMIN
        )


async def test_demote_last_org_admin_raises(two_orgs, app_session):
    org_a, _ = two_orgs
    await repo.add_member(
        app_session, email="a@x.com", org_id=org_a, role=AdminRole.ADMIN, added_by="t"
    )
    with pytest.raises(repo.LastOrgAdminError):
        await repo.set_member_role(
            app_session, email="a@x.com", org_id=org_a, role=AdminRole.VIEWER
        )


async def test_remove_member(two_orgs, app_session):
    org_a, _ = two_orgs
    await repo.add_member(
        app_session, email="a@x.com", org_id=org_a, role=AdminRole.ADMIN, added_by="t"
    )
    await repo.add_member(
        app_session, email="b@x.com", org_id=org_a, role=AdminRole.ADMIN, added_by="t"
    )
    assert await repo.remove_member(app_session, email="b@x.com", org_id=org_a) is True
    assert await repo.get_membership(app_session, "b@x.com", org_a) is None


async def test_remove_missing_member_returns_false(two_orgs, app_session):
    org_a, _ = two_orgs
    assert await repo.remove_member(app_session, email="ghost@x.com", org_id=org_a) is False


async def test_remove_last_org_admin_raises(two_orgs, app_session):
    org_a, _ = two_orgs
    await repo.add_member(
        app_session, email="a@x.com", org_id=org_a, role=AdminRole.ADMIN, added_by="t"
    )
    with pytest.raises(repo.LastOrgAdminError):
        await repo.remove_member(app_session, email="a@x.com", org_id=org_a)
