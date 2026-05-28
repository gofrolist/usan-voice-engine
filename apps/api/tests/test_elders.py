import uuid


def test_create_elder_returns_201(client):
    r = client.post(
        "/v1/elders",
        json={
            "name": "Ada",
            "phone_e164": "+15551112222",
            "timezone": "America/New_York",
            "metadata": {"floor": 3},
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Ada"
    assert body["metadata"] == {"floor": 3}
    assert uuid.UUID(body["id"])


def test_create_elder_duplicate_phone_returns_409(client):
    client.post(
        "/v1/elders",
        json={"name": "A", "phone_e164": "+15553334444", "timezone": "UTC"},
    )
    r = client.post(
        "/v1/elders",
        json={"name": "B", "phone_e164": "+15553334444", "timezone": "UTC"},
    )
    assert r.status_code == 409


def test_update_elder_returns_200(client):
    created = client.post(
        "/v1/elders",
        json={"name": "A", "phone_e164": "+15555556666", "timezone": "UTC"},
    )
    elder_id = created.json()["id"]
    r = client.put(f"/v1/elders/{elder_id}", json={"name": "Renamed"})
    assert r.status_code == 200
    assert r.json()["name"] == "Renamed"


def test_update_elder_metadata_remaps_to_meta(client):
    created = client.post(
        "/v1/elders",
        json={
            "name": "A",
            "phone_e164": "+15557778888",
            "timezone": "UTC",
            "metadata": {"floor": 1},
        },
    )
    elder_id = created.json()["id"]
    r = client.put(f"/v1/elders/{elder_id}", json={"metadata": {"floor": 5}})
    assert r.status_code == 200
    assert r.json()["metadata"] == {"floor": 5}


def test_update_elder_duplicate_phone_returns_409(client):
    client.post(
        "/v1/elders",
        json={"name": "A", "phone_e164": "+15551110000", "timezone": "UTC"},
    )
    other = client.post(
        "/v1/elders",
        json={"name": "B", "phone_e164": "+15552220000", "timezone": "UTC"},
    )
    other_id = other.json()["id"]
    r = client.put(f"/v1/elders/{other_id}", json={"phone_e164": "+15551110000"})
    assert r.status_code == 409


def test_update_missing_elder_returns_404(client):
    r = client.put(f"/v1/elders/{uuid.uuid4()}", json={"name": "X"})
    assert r.status_code == 404
