"""Native /v1/admin/knowledge-bases API: CRUD, role gating, source lifecycle, RLS isolation."""

import asyncio

from tests.kb_helpers import _seed_kb_for_org
from tests.test_rls_p2_isolation import (  # noqa: F401 (fixtures discovered by pytest)
    _act_as_cookie,
    isolation_client,
)


def test_source_create_trims_title_and_text():
    # Both fields are normalized (trimmed) before persistence — text used to keep its
    # surrounding whitespace while title was trimmed; they are now consistent.
    from usan_api.schemas.admin_knowledge_bases import KbSourceCreate

    s = KbSourceCreate(title="  Doc  ", text="  hello world  ")
    assert s.title == "Doc"
    assert s.text == "hello world"


def test_create_then_get_detail(client, admin_session):
    r = client.post("/v1/admin/knowledge-bases", json={"name": "Wellness FAQ"})
    assert r.status_code == 201, r.text
    kb = r.json()
    assert kb["name"] == "Wellness FAQ"
    assert kb["status"] == "in_progress"
    assert kb["sources"] == []
    kb_id = kb["id"]

    detail = client.get(f"/v1/admin/knowledge-bases/{kb_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == kb_id


def test_list_reports_source_count(client, admin_session):
    kb_id = client.post("/v1/admin/knowledge-bases", json={"name": "KB"}).json()["id"]
    client.post(
        f"/v1/admin/knowledge-bases/{kb_id}/sources",
        json={"title": "Doc", "text": "hello"},
    )
    lst = client.get("/v1/admin/knowledge-bases")
    assert lst.status_code == 200
    row = next(k for k in lst.json() if k["id"] == kb_id)
    assert row["source_count"] == 1


def test_add_source_resets_to_in_progress_and_lists_pending(client, admin_session):
    kb_id = client.post("/v1/admin/knowledge-bases", json={"name": "KB"}).json()["id"]
    r = client.post(
        f"/v1/admin/knowledge-bases/{kb_id}/sources",
        json={"title": "Doc A", "text": "the content"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "in_progress"
    assert len(body["sources"]) == 1
    assert body["sources"][0]["title"] == "Doc A"
    assert body["sources"][0]["status"] == "pending"  # no chunks yet
    assert "content" not in body["sources"][0]  # raw text never echoed


def test_empty_source_title_422(client, admin_session):
    kb_id = client.post("/v1/admin/knowledge-bases", json={"name": "KB"}).json()["id"]
    r = client.post(
        f"/v1/admin/knowledge-bases/{kb_id}/sources",
        json={"title": "   ", "text": "x"},
    )
    assert r.status_code == 422


def test_delete_source_then_delete_kb(client, admin_session):
    kb_id = client.post("/v1/admin/knowledge-bases", json={"name": "KB"}).json()["id"]
    sid = client.post(
        f"/v1/admin/knowledge-bases/{kb_id}/sources",
        json={"title": "Doc", "text": "x"},
    ).json()["sources"][0]["id"]
    assert client.delete(f"/v1/admin/knowledge-bases/{kb_id}/sources/{sid}").status_code == 204
    assert client.delete(f"/v1/admin/knowledge-bases/{kb_id}").status_code == 204
    assert client.get(f"/v1/admin/knowledge-bases/{kb_id}").status_code == 404


def test_delete_kb_with_sources_cascades(client, admin_session):
    kb_id = client.post("/v1/admin/knowledge-bases", json={"name": "KB"}).json()["id"]
    r = client.post(
        f"/v1/admin/knowledge-bases/{kb_id}/sources",
        json={"title": "Doc", "text": "x"},
    )
    assert r.status_code == 201, r.text
    # Source is left in place; deleting the KB directly must cascade.
    assert client.delete(f"/v1/admin/knowledge-bases/{kb_id}").status_code == 204
    assert client.get(f"/v1/admin/knowledge-bases/{kb_id}").status_code == 404


def test_get_unknown_kb_404(client, admin_session):
    assert (
        client.get("/v1/admin/knowledge-bases/00000000-0000-0000-0000-000000000000").status_code
        == 404
    )


def test_create_requires_session(client):
    assert client.post("/v1/admin/knowledge-bases", json={"name": "KB"}).status_code == 401


def test_viewer_cannot_create(client, async_database_url):
    # Reuse the viewer-cookie helper pattern from test_admin_contacts_crud_api.py.
    from tests.test_admin_contacts_crud_api import _viewer_cookie

    _viewer_cookie(client, async_database_url, email="viewer-kb@example.com")
    assert client.post("/v1/admin/knowledge-bases", json={"name": "KB"}).status_code == 403
    # ...but a viewer CAN read.
    assert client.get("/v1/admin/knowledge-bases").status_code == 200


def test_cross_org_kb_is_404(isolation_client, two_orgs):  # noqa: F811
    client, super_url = isolation_client
    org_a, org_b = two_orgs
    from tests.test_rls_p2_isolation import _seed_super_admin

    asyncio.run(_seed_super_admin(super_url, "staff@usan.com"))
    kb_b = asyncio.run(_seed_kb_for_org(super_url, org_b, "Org B KB"))
    try:
        # Acting-as org A, org B's KB must be invisible (404, not 200/500).
        r = client.get(
            f"/v1/admin/knowledge-bases/{kb_b}",
            cookies=_act_as_cookie("staff@usan.com", org_a),
        )
        assert r.status_code == 404
        lst = client.get(
            "/v1/admin/knowledge-bases",
            cookies=_act_as_cookie("staff@usan.com", org_a),
        )
        assert str(kb_b) not in {k["id"] for k in lst.json()}
    finally:
        from tests.kb_helpers import _delete_kbs_for_org

        asyncio.run(_delete_kbs_for_org(super_url, org_b))
