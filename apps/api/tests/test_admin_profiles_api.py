import uuid

_OP = {"Authorization": "Bearer " + "o" * 32}


def _name() -> str:
    return f"profile-{uuid.uuid4().hex}"


def test_create_profile_returns_201(client):
    r = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP)
    assert r.status_code == 201
    body = r.json()
    assert body["published_version"] is None
    assert body["has_unpublished_draft"] is True


def test_create_profile_requires_operator_token(client):
    r = client.post("/v1/admin/profiles", json={"name": _name()})
    assert r.status_code == 401


def test_create_duplicate_name_returns_409(client):
    name = _name()
    assert client.post("/v1/admin/profiles", json={"name": name}, headers=_OP).status_code == 201
    r = client.post("/v1/admin/profiles", json={"name": name}, headers=_OP)
    assert r.status_code == 409


def test_create_with_unknown_clone_from_returns_404(client):
    r = client.post(
        "/v1/admin/profiles",
        json={"name": _name(), "clone_from": str(uuid.uuid4())},
        headers=_OP,
    )
    assert r.status_code == 404


def test_publish_then_list_versions(client):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP).json()["id"]
    r = client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "first"}, headers=_OP)
    assert r.status_code == 201
    assert r.json()["version"] == 1
    versions = client.get(f"/v1/admin/profiles/{pid}/versions", headers=_OP).json()
    assert len(versions) == 1
    assert versions[0]["note"] == "first"


def test_edit_draft_then_get_reflects_change(client):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP).json()["id"]
    detail = client.get(f"/v1/admin/profiles/{pid}", headers=_OP).json()
    cfg = detail["draft_config"]
    cfg["prompts"]["greeting"] = "Hi there, this is your check-in."
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg}, headers=_OP)
    assert r.status_code == 200
    assert r.json()["draft_config"]["prompts"]["greeting"] == "Hi there, this is your check-in."


def test_draft_rejects_brace_in_prompt(client):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP).json()["id"]
    cfg = client.get(f"/v1/admin/profiles/{pid}", headers=_OP).json()["draft_config"]
    cfg["prompts"]["greeting"] = "Hello {name}"
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg}, headers=_OP)
    assert r.status_code == 422


def test_rollback_creates_new_version(client):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP).json()["id"]
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"}, headers=_OP)
    cfg = client.get(f"/v1/admin/profiles/{pid}", headers=_OP).json()["draft_config"]
    cfg["prompts"]["greeting"] = "Changed greeting here."
    client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg}, headers=_OP)
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v2"}, headers=_OP)
    r = client.post(f"/v1/admin/profiles/{pid}/rollback/1", json={}, headers=_OP)
    assert r.status_code == 201
    assert r.json()["version"] == 3


def test_set_default_exclusive(client):
    a = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP).json()["id"]
    b = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP).json()["id"]
    payload = {"direction": "outbound"}
    r_a = client.post(f"/v1/admin/profiles/{a}/set-default", json=payload, headers=_OP)
    assert r_a.status_code == 200
    r_b = client.post(f"/v1/admin/profiles/{b}/set-default", json=payload, headers=_OP)
    assert r_b.status_code == 200
    profiles = {p["id"]: p for p in client.get("/v1/admin/profiles", headers=_OP).json()}
    assert profiles[a]["is_default_outbound"] is False
    assert profiles[b]["is_default_outbound"] is True


def test_archive_blocked_when_default_returns_409(client):
    pid = client.post("/v1/admin/profiles", json={"name": _name()}, headers=_OP).json()["id"]
    client.post(f"/v1/admin/profiles/{pid}/set-default", json={"direction": "inbound"}, headers=_OP)
    r = client.post(f"/v1/admin/profiles/{pid}/archive", json={}, headers=_OP)
    assert r.status_code == 409


def test_get_missing_profile_returns_404(client):
    r = client.get(f"/v1/admin/profiles/{uuid.uuid4()}", headers=_OP)
    assert r.status_code == 404
