import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.models import AdminAuditLog


def _name() -> str:
    return f"profile-{uuid.uuid4().hex}"


async def _fetch_audit(async_database_url: str, action: str) -> AdminAuditLog | None:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            result = await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == action))
            return result.scalars().first()
    finally:
        await engine.dispose()


def test_create_profile_returns_201(client, admin_session):
    r = client.post("/v1/admin/profiles", json={"name": _name()})
    assert r.status_code == 201
    body = r.json()
    assert body["published_version"] is None
    assert body["has_unpublished_draft"] is True


def test_create_profile_requires_session(client):
    # No session cookie: the management plane rejects the request.
    r = client.post("/v1/admin/profiles", json={"name": _name()})
    assert r.status_code == 401


def test_create_duplicate_name_returns_409(client, admin_session):
    name = _name()
    assert client.post("/v1/admin/profiles", json={"name": name}).status_code == 201
    r = client.post("/v1/admin/profiles", json={"name": name})
    assert r.status_code == 409


def test_create_with_unknown_clone_from_returns_404(client, admin_session):
    r = client.post(
        "/v1/admin/profiles",
        json={"name": _name(), "clone_from": str(uuid.uuid4())},
    )
    assert r.status_code == 404


def test_publish_then_list_versions(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    r = client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "first"})
    assert r.status_code == 201
    assert r.json()["version"] == 1
    versions = client.get(f"/v1/admin/profiles/{pid}/versions").json()
    assert len(versions) == 1
    assert versions[0]["note"] == "first"


def test_edit_draft_then_get_reflects_change(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    detail = client.get(f"/v1/admin/profiles/{pid}").json()
    cfg = detail["draft_config"]
    cfg["prompts"]["greeting"] = "Hi there, this is your check-in."
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    assert r.status_code == 200
    assert r.json()["draft_config"]["prompts"]["greeting"] == "Hi there, this is your check-in."


def test_draft_rejects_brace_in_prompt(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    cfg = client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]
    cfg["prompts"]["greeting"] = "Hello {name}"
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    assert r.status_code == 422


def test_rollback_creates_new_version(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"})
    cfg = client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]
    cfg["prompts"]["greeting"] = "Changed greeting here."
    client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v2"})
    r = client.post(f"/v1/admin/profiles/{pid}/rollback/1", json={})
    assert r.status_code == 201
    assert r.json()["version"] == 3


def test_set_default_exclusive(client, admin_session):
    a = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    b = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    payload = {"direction": "outbound"}
    assert client.post(f"/v1/admin/profiles/{a}/set-default", json=payload).status_code == 200
    assert client.post(f"/v1/admin/profiles/{b}/set-default", json=payload).status_code == 200
    profiles = {p["id"]: p for p in client.get("/v1/admin/profiles").json()}
    assert profiles[a]["is_default_outbound"] is False
    assert profiles[b]["is_default_outbound"] is True


def test_archive_blocked_when_default_returns_409(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    client.post(f"/v1/admin/profiles/{pid}/set-default", json={"direction": "inbound"})
    r = client.post(f"/v1/admin/profiles/{pid}/archive", json={})
    assert r.status_code == 409


def test_get_missing_profile_returns_404(client, admin_session):
    r = client.get(f"/v1/admin/profiles/{uuid.uuid4()}")
    assert r.status_code == 404


def test_list_versions_unknown_profile_returns_404(client, admin_session):
    r = client.get(f"/v1/admin/profiles/{uuid.uuid4()}/versions")
    assert r.status_code == 404


def test_update_draft_unknown_profile_returns_404(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    cfg = client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]
    r = client.put(f"/v1/admin/profiles/{uuid.uuid4()}/draft", json={"config": cfg})
    assert r.status_code == 404


def test_publish_unknown_profile_returns_404(client, admin_session):
    r = client.post(f"/v1/admin/profiles/{uuid.uuid4()}/publish", json={"note": "x"})
    assert r.status_code == 404


def test_rollback_unknown_version_returns_404(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"})
    r = client.post(f"/v1/admin/profiles/{pid}/rollback/999", json={})
    assert r.status_code == 404


def test_get_version_returns_config_and_404(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    draft = client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"})
    r = client.get(f"/v1/admin/profiles/{pid}/versions/1")
    assert r.status_code == 200
    assert r.json()["config"] == draft
    missing = client.get(f"/v1/admin/profiles/{pid}/versions/999")
    assert missing.status_code == 404


def test_publish_records_audit_entry_with_session_actor(client, admin_session, async_database_url):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    r = client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"})
    assert r.status_code == 201
    entry = asyncio.run(_fetch_audit(async_database_url, "profile.publish"))
    assert entry is not None
    # Actor is now the authenticated operator's email, not the pre-SSO sentinel.
    assert entry.actor_email == "admin@example.com"
    assert entry.detail == {"version": 1}


def test_rollback_records_audit_entry(client, admin_session, async_database_url):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"})
    cfg = client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]
    cfg["llm"]["temperature"] = 0.4
    client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v2"})
    r = client.post(f"/v1/admin/profiles/{pid}/rollback/1", json={})
    assert r.status_code == 201
    entry = asyncio.run(_fetch_audit(async_database_url, "profile.rollback"))
    assert entry is not None
    assert entry.actor_email
    # Rolling v1 back re-publishes it as a new version (v3): detail records both ends.
    assert entry.detail == {"from_version": 1, "new_version": 3}


def test_set_default_unknown_profile_returns_404(client, admin_session):
    r = client.post(
        f"/v1/admin/profiles/{uuid.uuid4()}/set-default",
        json={"direction": "inbound"},
    )
    assert r.status_code == 404


def test_archive_unknown_profile_returns_404(client, admin_session):
    r = client.post(f"/v1/admin/profiles/{uuid.uuid4()}/archive", json={})
    assert r.status_code == 404


def test_draft_save_returns_unknown_token_warnings(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    cfg = client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]
    # Known built-in + two unknown tokens across two fields.
    cfg["prompts"]["greeting"] = "Hello {{first_name}}, special {{promo}}!"
    cfg["prompts"]["system_prompt"] = cfg["prompts"]["system_prompt"] + "\nTone: {{mood_hint}}"
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    assert r.status_code == 200
    body = r.json()
    # Additive field: present, lists the unknown names, never the known built-in.
    assert set(body["warnings"]) == {"promo", "mood_hint"}


def test_draft_save_clean_config_has_empty_warnings(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    cfg = client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]
    cfg["prompts"]["greeting"] = "Hello {{first_name}}, this is your check-in."
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    assert r.status_code == 200
    assert r.json()["warnings"] == []


def test_get_profile_detail_warnings_defaults_empty(client, admin_session):
    # The additive field defaults to [] on GET (no warning computation there).
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    r = client.get(f"/v1/admin/profiles/{pid}")
    assert r.status_code == 200
    assert r.json()["warnings"] == []


def test_draft_save_returns_both_unknown_token_and_phi_warnings(client, admin_session):
    # PUT /draft with greeting={{last_check_in}} (PHI in sensitive field) AND an
    # unknown {{var}} — both warning types must appear, unknown-token names first.
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    cfg = client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]
    cfg["prompts"]["greeting"] = "Hello {{last_check_in}}, {{unknownvar}} here."
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    assert r.status_code == 200
    warnings = r.json()["warnings"]
    # Unknown token name comes first (existing behavior preserved).
    assert "unknownvar" in warnings
    # PHI advisory sentence must also be present.
    phi_warnings = [w for w in warnings if "{{last_check_in}}" in w and "greeting" in w]
    assert len(phi_warnings) == 1
    # Unknown-var entry precedes PHI advisory in the list.
    unknown_idx = next(i for i, w in enumerate(warnings) if w == "unknownvar")
    phi_idx = next(i for i, w in enumerate(warnings) if "{{last_check_in}}" in w)
    assert unknown_idx < phi_idx
