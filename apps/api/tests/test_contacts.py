import uuid

# Management-plane routes require the operator bearer token (matches conftest's
# OPERATOR_API_KEY). Kept as a module constant so each request stays terse.
_OP = {"Authorization": "Bearer " + "o" * 32}


def test_create_contact_returns_201(client):
    r = client.post(
        "/v1/contacts",
        json={
            "name": "Ada",
            "phone_e164": "+15551112222",
            "timezone": "America/New_York",
            "metadata": {"floor": 3},
        },
        headers=_OP,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Ada"
    assert body["metadata"] == {"floor": 3}
    assert uuid.UUID(body["id"])


def test_create_contact_duplicate_phone_returns_409(client):
    client.post(
        "/v1/contacts",
        json={"name": "A", "phone_e164": "+15553334444", "timezone": "UTC"},
        headers=_OP,
    )
    r = client.post(
        "/v1/contacts",
        json={"name": "B", "phone_e164": "+15553334444", "timezone": "UTC"},
        headers=_OP,
    )
    assert r.status_code == 409


def test_update_contact_returns_200(client):
    created = client.post(
        "/v1/contacts",
        json={"name": "A", "phone_e164": "+15555556666", "timezone": "UTC"},
        headers=_OP,
    )
    contact_id = created.json()["id"]
    r = client.put(f"/v1/contacts/{contact_id}", json={"name": "Renamed"}, headers=_OP)
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"


def test_update_contact_metadata_remaps_to_meta(client):
    created = client.post(
        "/v1/contacts",
        json={
            "name": "A",
            "phone_e164": "+15557778888",
            "timezone": "UTC",
            "metadata": {"floor": 1},
        },
        headers=_OP,
    )
    contact_id = created.json()["id"]
    r = client.put(f"/v1/contacts/{contact_id}", json={"metadata": {"floor": 5}}, headers=_OP)
    assert r.status_code == 200
    assert r.json()["metadata"] == {"floor": 5}


def test_update_contact_duplicate_phone_returns_409(client):
    client.post(
        "/v1/contacts",
        json={"name": "A", "phone_e164": "+15551110000", "timezone": "UTC"},
        headers=_OP,
    )
    other = client.post(
        "/v1/contacts",
        json={"name": "B", "phone_e164": "+15552220000", "timezone": "UTC"},
        headers=_OP,
    )
    other_id = other.json()["id"]
    r = client.put(f"/v1/contacts/{other_id}", json={"phone_e164": "+15551110000"}, headers=_OP)
    assert r.status_code == 409


def test_update_missing_contact_returns_404(client):
    r = client.put(f"/v1/contacts/{uuid.uuid4()}", json={"name": "X"}, headers=_OP)
    assert r.status_code == 404


def test_create_contact_invalid_phone_returns_422(client):
    r = client.post(
        "/v1/contacts",
        json={"name": "A", "phone_e164": "5551234567", "timezone": "UTC"},
        headers=_OP,
    )
    assert r.status_code == 422


def test_create_contact_sip_uri_phone_rejected_422(client):
    r = client.post(
        "/v1/contacts",
        json={"name": "A", "phone_e164": "sip:victim@attacker.com", "timezone": "UTC"},
        headers=_OP,
    )
    assert r.status_code == 422


def test_update_contact_invalid_phone_returns_422(client):
    created = client.post(
        "/v1/contacts",
        json={"name": "A", "phone_e164": "+15551239999", "timezone": "UTC"},
        headers=_OP,
    )
    contact_id = created.json()["id"]
    r = client.put(f"/v1/contacts/{contact_id}", json={"phone_e164": "not-a-number"}, headers=_OP)
    assert r.status_code == 422


def test_create_contact_oversized_name_returns_422(client):
    r = client.post(
        "/v1/contacts",
        json={"name": "A" * 201, "phone_e164": "+15551230001", "timezone": "UTC"},
        headers=_OP,
    )
    assert r.status_code == 422


def test_create_contact_requires_operator_token(client):
    payload = {"name": "A", "phone_e164": "+15551239000", "timezone": "UTC"}
    assert client.post("/v1/contacts", json=payload).status_code == 401
    wrong = {"Authorization": "Bearer " + "x" * 32}
    assert client.post("/v1/contacts", json=payload, headers=wrong).status_code == 401


def test_update_contact_requires_operator_token(client):
    created = client.post(
        "/v1/contacts",
        json={"name": "A", "phone_e164": "+15551239001", "timezone": "UTC"},
        headers=_OP,
    )
    contact_id = created.json()["id"]
    assert client.put(f"/v1/contacts/{contact_id}", json={"name": "Z"}).status_code == 401
    wrong = {"Authorization": "Bearer " + "x" * 32}
    r = client.put(f"/v1/contacts/{contact_id}", json={"name": "Z"}, headers=wrong)
    assert r.status_code == 401
