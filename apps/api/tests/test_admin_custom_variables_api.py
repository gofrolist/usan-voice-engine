"""API tests for /v1/admin/custom-variables (spec §5).

Mirrors test_admin_users_api.py: router-level session gate, ADMIN-role write
gating, repo-domain 409s, and admin_audit rows (asserted through the
/v1/admin/audit read API, which echoes `detail`).
"""

import asyncio
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.admin_session import SESSION_COOKIE_NAME, issue_session
from usan_api.db.base import AdminRole
from usan_api.settings import get_settings

BASE = "/v1/admin/custom-variables"


async def _seed(async_database_url: str, email: str, role: str) -> None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO admin_users (email, role, added_by) "
                    "VALUES (:e, CAST(:r AS admin_role), 'test') "
                    "ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role"
                ),
                {"e": email.lower(), "r": role},
            )
    finally:
        await engine.dispose()


def _create(client, name: str, **overrides):
    body = {"name": name, "description": "", "example": "", "phi": False, **overrides}
    return client.post(BASE, json=body)


def test_list_requires_session(client):
    assert client.get(BASE).status_code == 401


def test_create_and_list_alphabetical(client, admin_session):
    z = _create(client, "zebra_var", description="dz", example="ez")
    assert z.status_code == 201
    a = _create(client, "apple_var", phi=True)
    assert a.status_code == 201

    # Full CustomVariableOut echo on create.
    created = z.json()
    assert set(created) == {
        "id",
        "name",
        "description",
        "example",
        "phi",
        "created_at",
        "updated_at",
    }
    assert created["name"] == "zebra_var"
    assert created["description"] == "dz"
    assert created["example"] == "ez"
    assert created["phi"] is False
    assert a.json()["phi"] is True

    listed = client.get(BASE)
    assert listed.status_code == 200
    assert [v["name"] for v in listed.json()] == ["apple_var", "zebra_var"]


def test_create_duplicate_409(client, admin_session):
    assert _create(client, "pet_name").status_code == 201
    dup = _create(client, "pet_name")
    assert dup.status_code == 409
    assert "pet_name" in dup.json()["detail"]


def test_create_bad_slug_422(client, admin_session):
    for bad in ("Bad", "9starts_with_digit", "has space", "has-dash", "a" * 65, ""):
        assert _create(client, bad).status_code == 422, bad


def test_create_builtin_collision_422(client, admin_session):
    r = _create(client, "elder_name")
    assert r.status_code == 422
    assert "builtin" in json.dumps(r.json())


def test_patch_edits_description_example_phi(client, admin_session):
    vid = _create(client, "pet_name", description="d1").json()["id"]
    r = client.patch(f"{BASE}/{vid}", json={"phi": True, "description": "d2"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "pet_name"  # immutable
    assert body["description"] == "d2"
    assert body["phi"] is True
    assert body["example"] == ""  # untouched


def test_patch_rejects_name_422(client, admin_session):
    vid = _create(client, "pet_name").json()["id"]
    r = client.patch(f"{BASE}/{vid}", json={"name": "other"})
    assert r.status_code == 422  # extra="forbid" — name is immutable after create
    assert client.get(BASE).json()[0]["name"] == "pet_name"


def test_patch_unknown_404(client, admin_session):
    r = client.patch(f"{BASE}/00000000-0000-0000-0000-000000000000", json={"phi": True})
    assert r.status_code == 404


def test_delete_unknown_404(client, admin_session):
    r = client.delete(f"{BASE}/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404


def test_delete_204(client, admin_session):
    vid = _create(client, "pet_name").json()["id"]
    assert client.delete(f"{BASE}/{vid}").status_code == 204
    assert client.get(BASE).json() == []


def test_viewer_cannot_mutate_403(client, async_database_url):
    # Mutations are ADMIN-gated; a viewer can read the catalog definitions only.
    asyncio.run(_seed(async_database_url, "viewer@example.com", "viewer"))
    token = issue_session("viewer@example.com", AdminRole.VIEWER, get_settings())
    client.cookies.set(SESSION_COOKIE_NAME, token)
    assert _create(client, "pet_name").status_code == 403
    fake = "00000000-0000-0000-0000-000000000000"
    assert client.patch(f"{BASE}/{fake}", json={"phi": True}).status_code == 403
    assert client.delete(f"{BASE}/{fake}").status_code == 403
    assert client.get(BASE).status_code == 200


def test_mutations_audited_with_phi_old_new_detail(client, admin_session):
    vid = _create(client, "pet_name").json()["id"]
    assert client.patch(f"{BASE}/{vid}", json={"phi": True, "description": "d2"}).status_code == 200
    assert client.delete(f"{BASE}/{vid}").status_code == 204

    entries = client.get("/v1/admin/audit").json()
    rows = [e for e in entries if e["entity_type"] == "custom_variable"]
    by_action = {e["action"]: e for e in rows}
    assert set(by_action) == {
        "custom_variable.create",
        "custom_variable.update",
        "custom_variable.delete",
    }
    assert all(e["actor_email"] == "admin@example.com" for e in rows)
    assert all(e["entity_id"] == vid for e in rows)

    # The update row pins the phi transition + the changed-field names.
    update_detail = by_action["custom_variable.update"]["detail"]
    assert update_detail["phi"] == {"old": False, "new": True}
    assert set(update_detail["changed"]) == {"description", "phi"}

    # Definitions are operator config: audit detail carries names/flags only —
    # never per-call values (spec §5/§7).
    assert "dynamic_vars" not in json.dumps([e["detail"] for e in rows])
