# Management-plane routes require the operator bearer token (matches conftest's
from tests.conftest import OPERATOR_HEADERS as _OP

# OPERATOR_API_KEY).


def test_add_dnc_returns_201(client):
    r = client.post(
        "/v1/dnc", json={"phone_e164": "+15550001111", "reason": "requested"}, headers=_OP
    )
    assert r.status_code == 201
    body = r.json()
    assert body["phone_e164"] == "+15550001111"
    assert body["reason"] == "requested"


def test_add_dnc_is_idempotent_upsert(client):
    client.post("/v1/dnc", json={"phone_e164": "+15550002222", "reason": "a"}, headers=_OP)
    r = client.post("/v1/dnc", json={"phone_e164": "+15550002222", "reason": "b"}, headers=_OP)
    assert r.status_code == 201
    assert r.json()["reason"] == "b"


def test_remove_dnc_returns_204_then_404(client):
    client.post("/v1/dnc", json={"phone_e164": "+15550003333", "reason": None}, headers=_OP)
    d1 = client.delete("/v1/dnc/%2B15550003333", headers=_OP)
    assert d1.status_code == 204
    d2 = client.delete("/v1/dnc/%2B15550003333", headers=_OP)
    assert d2.status_code == 404


def test_add_dnc_invalid_phone_returns_422(bare_client):
    r = bare_client.post("/v1/dnc", json={"phone_e164": "5550001111", "reason": "x"}, headers=_OP)
    assert r.status_code == 422


def test_add_dnc_requires_operator_token(bare_client):
    payload = {"phone_e164": "+15550009999", "reason": "x"}
    assert bare_client.post("/v1/dnc", json=payload).status_code == 401
    wrong = {"Authorization": "Bearer " + "x" * 32}
    assert bare_client.post("/v1/dnc", json=payload, headers=wrong).status_code == 401


def test_remove_dnc_requires_operator_token(bare_client):
    assert bare_client.delete("/v1/dnc/%2B15550008888").status_code == 401
    wrong = {"Authorization": "Bearer " + "x" * 32}
    assert bare_client.delete("/v1/dnc/%2B15550008888", headers=wrong).status_code == 401


def test_remove_dnc_rejects_malformed_phone(bare_client):
    # The DELETE path param is held to the same E.164 contract as the POST body.
    assert bare_client.delete("/v1/dnc/not-a-phone", headers=_OP).status_code == 422
    assert bare_client.delete("/v1/dnc/5550001111", headers=_OP).status_code == 422
