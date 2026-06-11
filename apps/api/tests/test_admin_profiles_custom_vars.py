"""Save-path warnings with declared custom variables (spec §3.2, plan A4 task C5).

Declared customs stop warning as "unknown"; undeclared tokens keep warning;
custom phi=true names get the same sensitive-field advisory as builtin PHI.
All warn-don't-block: the save itself succeeds (200) — the prompt channel has
no fail-closed defense, so the warning IS the defense.
"""

import uuid


def _name() -> str:
    return f"profile-{uuid.uuid4().hex}"


def _create_custom(client, name: str, *, phi: bool = False) -> None:
    r = client.post(
        "/v1/admin/custom-variables",
        json={"name": name, "description": "", "example": "", "phi": phi},
    )
    assert r.status_code == 201


def _draft_config(client, pid: str) -> dict:
    return client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]


def test_declared_custom_absent_from_unknown_warnings(client, admin_session):
    _create_custom(client, "pet_name")
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    cfg = _draft_config(client, pid)
    cfg["prompts"]["greeting"] = "Hello {{first_name}}, how is {{pet_name}} today?"
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    assert r.status_code == 200
    assert "pet_name" not in r.json()["warnings"]


def test_undeclared_token_still_warns(client, admin_session):
    _create_custom(client, "pet_name")
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    cfg = _draft_config(client, pid)
    cfg["prompts"]["greeting"] = "Hello {{pet_name}}, about {{mystery}}."
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    assert r.status_code == 200
    assert "mystery" in r.json()["warnings"]


def test_custom_phi_in_sensitive_field_warns(client, admin_session):
    # Custom phi=true token in voicemail_message: same advisory as builtin PHI,
    # warn-don't-block — the save still returns 200.
    _create_custom(client, "diagnosis", phi=True)
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    cfg = _draft_config(client, pid)
    cfg["prompts"]["voicemail_message"] = "We will follow up about {{diagnosis}} soon."
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    assert r.status_code == 200
    warnings = r.json()["warnings"]
    matching = [w for w in warnings if "{{diagnosis}}" in w and "'voicemail_message'" in w]
    assert len(matching) == 1
