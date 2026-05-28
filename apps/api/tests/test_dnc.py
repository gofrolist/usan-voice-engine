def test_add_dnc_returns_201(client):
    r = client.post("/v1/dnc", json={"phone_e164": "+15550001111", "reason": "requested"})
    assert r.status_code == 201
    body = r.json()
    assert body["phone_e164"] == "+15550001111"
    assert body["reason"] == "requested"


def test_add_dnc_is_idempotent_upsert(client):
    client.post("/v1/dnc", json={"phone_e164": "+15550002222", "reason": "a"})
    r = client.post("/v1/dnc", json={"phone_e164": "+15550002222", "reason": "b"})
    assert r.status_code == 201
    assert r.json()["reason"] == "b"


def test_remove_dnc_returns_204_then_404(client):
    client.post("/v1/dnc", json={"phone_e164": "+15550003333", "reason": None})
    d1 = client.delete("/v1/dnc/%2B15550003333")
    assert d1.status_code == 204
    d2 = client.delete("/v1/dnc/%2B15550003333")
    assert d2.status_code == 404
