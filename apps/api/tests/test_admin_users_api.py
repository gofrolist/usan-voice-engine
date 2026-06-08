def test_list_includes_self(client, admin_session):
    r = client.get("/v1/admin/admin-users")
    assert r.status_code == 200
    emails = {u["email"] for u in r.json()}
    assert "admin@example.com" in emails


def test_add_then_remove_admin_user(client, admin_session):
    add = client.post("/v1/admin/admin-users", json={"email": "Bob@Example.com", "role": "viewer"})
    assert add.status_code == 201
    assert add.json()["email"] == "bob@example.com"
    assert add.json()["role"] == "viewer"

    emails = {u["email"] for u in client.get("/v1/admin/admin-users").json()}
    assert "bob@example.com" in emails

    rm = client.delete("/v1/admin/admin-users/bob@example.com")
    assert rm.status_code == 204
    emails = {u["email"] for u in client.get("/v1/admin/admin-users").json()}
    assert "bob@example.com" not in emails


def test_remove_unknown_returns_404(client, admin_session):
    r = client.delete("/v1/admin/admin-users/nobody@example.com")
    assert r.status_code == 404


def test_add_requires_session(client):
    r = client.post("/v1/admin/admin-users", json={"email": "x@y.com"})
    assert r.status_code == 401


def test_add_invalid_email_422(client, admin_session):
    r = client.post("/v1/admin/admin-users", json={"email": "not-an-email"})
    assert r.status_code == 422
