"""Task C2: the super-admin org console (/v1/admin/organizations).

These are platform-level (global, non-RLS) control-plane endpoints, so the router
depends on ``require_super_admin`` + ``get_db`` (NOT ``get_tenant_db``): a USAN
staffer lists every org and provisions a new one (optionally with a first ADMIN).
Non-super-admins are refused (403); a duplicate slug is a 409.
"""

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


def _slug(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


async def _membership_role(super_async_url: str, email: str, org_id: uuid.UUID) -> str | None:
    engine = create_async_engine(super_async_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            row = await conn.execute(
                text("SELECT role FROM memberships WHERE email = :e AND organization_id = :o"),
                {"e": email.lower(), "o": org_id},
            )
            return row.scalar_one_or_none()
    finally:
        await engine.dispose()


def test_super_admin_lists_all_orgs(client, super_admin_session, two_orgs):
    org_a, org_b = two_orgs
    resp = client.get("/v1/admin/organizations")
    assert resp.status_code == 200
    ids = {o["id"] for o in resp.json()}
    # Both seeded orgs (plus the connect-baseline usan org) are visible.
    assert str(org_a) in ids
    assert str(org_b) in ids


def test_super_admin_creates_org(client, super_admin_session):
    slug = _slug("acme")
    resp = client.post(
        "/v1/admin/organizations",
        json={"name": "Acme Retirement", "slug": slug},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "Acme Retirement"
    assert body["slug"] == slug
    assert body["status"] == "active"
    assert uuid.UUID(body["id"])  # valid uuid


def test_super_admin_creates_org_with_first_admin(client, super_admin_session, async_database_url):
    slug = _slug("beta")
    resp = client.post(
        "/v1/admin/organizations",
        json={"name": "Beta Care", "slug": slug, "first_admin_email": "Owner@Example.com"},
    )
    assert resp.status_code == 201
    org_id = uuid.UUID(resp.json()["id"])
    role = asyncio.run(_membership_role(async_database_url, "owner@example.com", org_id))
    assert role == "admin"


def test_non_super_admin_forbidden(client, admin_session):
    assert client.get("/v1/admin/organizations").status_code == 403
    assert (
        client.post(
            "/v1/admin/organizations",
            json={"name": "Nope", "slug": _slug("nope")},
        ).status_code
        == 403
    )


def test_duplicate_slug_conflict(client, super_admin_session):
    slug = _slug("dupe")
    first = client.post("/v1/admin/organizations", json={"name": "First", "slug": slug})
    assert first.status_code == 201
    second = client.post("/v1/admin/organizations", json={"name": "Second", "slug": slug})
    assert second.status_code == 409


def test_requires_session(bare_client):
    assert bare_client.get("/v1/admin/organizations").status_code == 401


def test_invalid_slug_422(client, super_admin_session):
    assert (
        client.post(
            "/v1/admin/organizations",
            json={"name": "Bad", "slug": "Bad Slug!"},
        ).status_code
        == 422
    )
