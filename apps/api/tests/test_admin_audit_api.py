def test_audit_requires_session(client):
    assert client.get("/v1/admin/audit").status_code == 401


def test_audit_lists_recent_entries(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": "p-audit"}).json()["id"]
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"})
    r = client.get("/v1/admin/audit")
    assert r.status_code == 200
    actions = {e["action"] for e in r.json()}
    assert "profile.publish" in actions
    assert all("actor_email" in e for e in r.json())


def test_audit_limit_bounds(client, admin_session):
    # limit is bounded by Query(le=500): above the cap is a 422, within is 200.
    assert client.get("/v1/admin/audit?limit=100000").status_code == 422
    assert client.get("/v1/admin/audit?limit=500").status_code == 200
