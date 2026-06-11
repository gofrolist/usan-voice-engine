"""Save-path warnings with declared custom variables (spec §3.2, plan A4 task C5)
and the custom-PHI-in-SMS 422 gate at save/publish/rollback (spec §3.2.1, task C6).

Declared customs stop warning as "unknown"; undeclared tokens keep warning;
custom phi=true names get the same sensitive-field advisory as builtin PHI.
All warn-don't-block on the prompt channel: the save itself succeeds (200) — the
prompt channel has no fail-closed defense, so the warning IS the defense. SMS is
the exception: a phi=true custom in an SMS body is a hard 422 (server-
authoritative; the client only shows a notice).
"""

import uuid


def _name() -> str:
    return f"profile-{uuid.uuid4().hex}"


def _create_custom(client, name: str, *, phi: bool = False) -> dict:
    r = client.post(
        "/v1/admin/custom-variables",
        json={"name": name, "description": "", "example": "", "phi": phi},
    )
    assert r.status_code == 201
    return r.json()


def _draft_config(client, pid: str) -> dict:
    return client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]


def _with_sms_bodies(cfg: dict, *bodies: str) -> dict:
    cfg["tools"]["sms"] = {
        "templates": [
            {"key": f"t{i}", "label": f"T{i}", "body": body} for i, body in enumerate(bodies)
        ]
    }
    return cfg


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


# --- custom-PHI-in-SMS 422 at save/publish/rollback (spec §3.2.1, plan C6) ----


def test_save_422_custom_phi_in_sms_body(client, admin_session):
    _create_custom(client, "diagnosis", phi=True)
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    before = _draft_config(client, pid)
    cfg = _with_sms_bodies(_draft_config(client, pid), "Your result: {{diagnosis}}")
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail[0]["loc"] == ["body", "config", "tools", "sms", "templates", 0, "body"]
    assert detail[0]["type"] == "value_error.custom_phi_sms"
    # Draft unchanged: the check runs BEFORE persistence.
    assert _draft_config(client, pid) == before


def test_publish_422_after_phi_flip(client, admin_session):
    # Save while x is phi=False (200 + renders-empty warning), flip to phi=True,
    # then publish — the helper re-runs on draft_config and 422s.
    var = _create_custom(client, "x")
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    cfg = _with_sms_bodies(_draft_config(client, pid), "Note about {{x}} today.")
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    assert r.status_code == 200
    assert (
        "{{x}} is not substituted in SMS — it will render as empty text." in (r.json()["warnings"])
    )
    r = client.patch(f"/v1/admin/custom-variables/{var['id']}", json={"phi": True})
    assert r.status_code == 200
    r = client.post(f"/v1/admin/profiles/{pid}/publish", json={})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail[0]["loc"] == ["body", "config", "tools", "sms", "templates", 0, "body"]
    assert detail[0]["type"] == "value_error.custom_phi_sms"


def test_rollback_422_when_snapshot_references_now_phi_custom(client, admin_session):
    # The no-pydantic-re-entry hole (spec §3.2.1): repo.rollback → repo.publish
    # republishes the old snapshot with no validation — the router gate must 422.
    var = _create_custom(client, "x")
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    # v1: snapshot referencing {{x}} in an SMS body (x is non-PHI at the time).
    cfg = _with_sms_bodies(_draft_config(client, pid), "Note about {{x}} today.")
    assert client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg}).status_code == 200
    assert client.post(f"/v1/admin/profiles/{pid}/publish", json={}).status_code == 201
    # v2: clean snapshot.
    clean = _with_sms_bodies(_draft_config(client, pid))
    assert client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": clean}).status_code == 200
    assert client.post(f"/v1/admin/profiles/{pid}/publish", json={}).status_code == 201
    # Flip x to phi=True, then roll back to the offending v1 -> 422.
    assert (
        client.patch(f"/v1/admin/custom-variables/{var['id']}", json={"phi": True}).status_code
        == 200
    )
    r = client.post(f"/v1/admin/profiles/{pid}/rollback/1")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail[0]["loc"] == ["body", "config", "tools", "sms", "templates", 0, "body"]
    assert detail[0]["type"] == "value_error.custom_phi_sms"
    # Rolling back to the clean v2 still works.
    assert client.post(f"/v1/admin/profiles/{pid}/rollback/2").status_code == 201


def test_save_warns_renders_empty_for_non_phi_custom_in_sms(client, admin_session):
    # Non-PHI custom AND undeclared tokens both warn (declared-vs-undeclared
    # parity, spec §3.2.1); builtin non-PHI tokens DO substitute, so no warning.
    _create_custom(client, "pet_name")
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    cfg = _with_sms_bodies(
        _draft_config(client, pid), "Hi {{first_name}}, {{pet_name}} and {{mystery2}}."
    )
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    assert r.status_code == 200
    warnings = r.json()["warnings"]
    assert "{{pet_name}} is not substituted in SMS — it will render as empty text." in warnings
    assert "{{mystery2}} is not substituted in SMS — it will render as empty text." in warnings
    assert not any("{{first_name}}" in w for w in warnings)
