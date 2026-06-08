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


def test_audit_action_filter_finds_match_beyond_limit_window(client, admin_session):
    # Compliance-screen fix: a server-side action filter must find a match that falls
    # OUTSIDE the latest-N window. Create one set_default, then several later publishes;
    # with a tiny unfiltered window the set_default is invisible, but the filter finds it.
    a = client.post("/v1/admin/profiles", json={"name": "p-aud-a"}).json()["id"]
    client.post(f"/v1/admin/profiles/{a}/set-default", json={"direction": "inbound"})
    for i in range(5):
        pid = client.post("/v1/admin/profiles", json={"name": f"p-aud-{i}"}).json()["id"]
        client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v"})
    recent = client.get("/v1/admin/audit?limit=3").json()
    assert all(e["action"] != "profile.set_default" for e in recent)  # outside the window
    filtered = client.get("/v1/admin/audit?action=profile.set_default&limit=3").json()
    assert filtered
    assert all(e["action"] == "profile.set_default" for e in filtered)


def test_audit_actor_filter(client, admin_session):
    pid = client.post("/v1/admin/profiles", json={"name": "p-actor"}).json()["id"]
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"})
    rows = client.get("/v1/admin/audit").json()
    assert rows
    actor = rows[0]["actor_email"]
    # A substring of the real actor matches (case-insensitive); a bogus one matches none.
    assert len(client.get(f"/v1/admin/audit?actor={actor[:4]}").json()) >= 1
    assert client.get("/v1/admin/audit?actor=zzz-no-such-actor").json() == []
