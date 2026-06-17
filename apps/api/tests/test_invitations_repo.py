from datetime import UTC, datetime, timedelta

import pytest

from usan_api.db.base import AdminRole, InviteStatus
from usan_api.repositories import invitations as repo
from usan_api.repositories import memberships as memberships_repo

# Fixtures `two_orgs`, `app_session` come from conftest (P2 additions).


async def test_create_and_list_pending(two_orgs, app_session):
    org_a, _ = two_orgs
    inv = await repo.create_invite(
        app_session,
        org_id=org_a,
        email="A@X.com",
        role=AdminRole.VIEWER,
        invited_by="boss@x.com",
        ttl_hours=168,
    )
    assert inv.email == "a@x.com"  # normalized
    assert inv.status is InviteStatus.PENDING
    assert inv.token  # non-empty
    assert inv.expires_at > datetime.now(UTC)
    pending = await repo.list_pending(app_session, org_a)
    assert [i.email for i in pending] == ["a@x.com"]


async def test_create_regenerates_existing_pending(two_orgs, app_session):
    org_a, _ = two_orgs
    first = await repo.create_invite(
        app_session,
        org_id=org_a,
        email="a@x.com",
        role=AdminRole.VIEWER,
        invited_by="b@x.com",
        ttl_hours=168,
    )
    first_token = first.token
    second = await repo.create_invite(
        app_session,
        org_id=org_a,
        email="a@x.com",
        role=AdminRole.ADMIN,
        invited_by="b@x.com",
        ttl_hours=168,
    )
    assert second.id == first.id  # same row, regenerated
    assert second.token != first_token
    assert len(await repo.list_pending(app_session, org_a)) == 1


async def test_create_rejects_existing_member(two_orgs, app_session):
    org_a, _ = two_orgs
    await memberships_repo.add_member(
        app_session, email="m@x.com", org_id=org_a, role=AdminRole.VIEWER, added_by="t"
    )
    with pytest.raises(repo.AlreadyMemberError):
        await repo.create_invite(
            app_session,
            org_id=org_a,
            email="m@x.com",
            role=AdminRole.ADMIN,
            invited_by="b@x.com",
            ttl_hours=168,
        )


async def test_get_invite_is_org_scoped(two_orgs, app_session):
    org_a, org_b = two_orgs
    inv = await repo.create_invite(
        app_session,
        org_id=org_a,
        email="a@x.com",
        role=AdminRole.VIEWER,
        invited_by="b@x.com",
        ttl_hours=168,
    )
    assert await repo.get_invite(app_session, inv.id, org_a) is not None
    assert await repo.get_invite(app_session, inv.id, org_b) is None  # other org can't see it


async def test_get_by_token(two_orgs, app_session):
    org_a, _ = two_orgs
    inv = await repo.create_invite(
        app_session,
        org_id=org_a,
        email="a@x.com",
        role=AdminRole.VIEWER,
        invited_by="b@x.com",
        ttl_hours=168,
    )
    found = await repo.get_by_token(app_session, inv.token)
    assert found is not None
    assert found.id == inv.id
    assert await repo.get_by_token(app_session, "nope") is None
    # for_update row-locks the invite for the accept path; same row, no behavior change
    # for a single session (the lock serializes concurrent accepts).
    locked = await repo.get_by_token(app_session, inv.token, for_update=True)
    assert locked is not None
    assert locked.id == inv.id


async def test_revoke(two_orgs, app_session):
    org_a, _ = two_orgs
    inv = await repo.create_invite(
        app_session,
        org_id=org_a,
        email="a@x.com",
        role=AdminRole.VIEWER,
        invited_by="b@x.com",
        ttl_hours=168,
    )
    await repo.revoke(app_session, inv)
    assert inv.status is InviteStatus.REVOKED
    with pytest.raises(repo.NotPendingError):
        await repo.revoke(app_session, inv)  # not pending anymore


async def test_resend_rotates_token_and_expiry(two_orgs, app_session):
    org_a, _ = two_orgs
    inv = await repo.create_invite(
        app_session,
        org_id=org_a,
        email="a@x.com",
        role=AdminRole.VIEWER,
        invited_by="b@x.com",
        ttl_hours=1,
    )
    old_token, old_exp = inv.token, inv.expires_at
    again = await repo.resend(app_session, inv, ttl_hours=168)
    assert again.token != old_token
    assert again.expires_at > old_exp


async def test_mark_accepted_and_usability(two_orgs, app_session):
    org_a, _ = two_orgs
    inv = await repo.create_invite(
        app_session,
        org_id=org_a,
        email="a@x.com",
        role=AdminRole.VIEWER,
        invited_by="b@x.com",
        ttl_hours=168,
    )
    now = datetime.now(UTC)
    assert repo.is_usable(inv, now=now) is True
    await repo.mark_accepted(app_session, inv)
    assert inv.status is InviteStatus.ACCEPTED
    assert inv.accepted_at is not None
    assert repo.is_usable(inv, now=now) is False  # accepted -> not usable


def test_is_usable_false_when_expired():
    # Pure helper test (no DB): a pending-but-expired invite is not usable.
    from types import SimpleNamespace

    past = datetime.now(UTC) - timedelta(hours=1)
    inv = SimpleNamespace(status=InviteStatus.PENDING, expires_at=past)
    assert repo.is_usable(inv, now=datetime.now(UTC)) is False
