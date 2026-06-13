"""Delete-guard reference scan for custom variables (US1 / FR-007).

``GET /v1/admin/custom-variables/{id}/references`` reports which profiles
reference the variable's ``{{name}}`` token, across the live draft AND every
immutable published version, so the UI can warn before a delete. It returns
names + locations only — never prompt text or per-call values (spec §7).
Written FIRST (Constitution IV).
"""

import uuid


def _name() -> str:
    return f"profile-{uuid.uuid4().hex}"


def _new_profile(client) -> str:
    return client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]


def _make_var(client, name: str, *, phi: bool = False) -> str:
    return client.post(
        "/v1/admin/custom-variables",
        json={"name": name, "description": "", "example": "", "phi": phi},
    ).json()["id"]


def _save_greeting(client, pid: str, text: str) -> None:
    cfg = client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]
    cfg["prompts"]["greeting"] = text
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    assert r.status_code == 200, r.text


def test_references_missing_variable_returns_404(client, admin_session):
    r = client.get(f"/v1/admin/custom-variables/{uuid.uuid4()}/references")
    assert r.status_code == 404


def test_unreferenced_variable_has_empty_profiles(client, admin_session):
    vid = _make_var(client, "promo")
    r = client.get(f"/v1/admin/custom-variables/{vid}/references")
    assert r.status_code == 200
    assert r.json()["profiles"] == []


def test_reference_in_draft_is_reported(client, admin_session):
    vid = _make_var(client, "promo")
    pid = _new_profile(client)
    _save_greeting(client, pid, "Hello, special {{promo}} today!")
    body = client.get(f"/v1/admin/custom-variables/{vid}/references").json()
    entries = {p["id"]: p for p in body["profiles"]}
    assert pid in entries
    assert any(w.startswith("draft") and "greeting" in w for w in entries[pid]["where"])


def test_reference_in_published_version_is_reported(client, admin_session):
    # The scan MUST include immutable version snapshots, not just the live draft.
    vid = _make_var(client, "promo")
    pid = _new_profile(client)
    _save_greeting(client, pid, "Published with {{promo}}.")
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"})
    # Move the draft off the token so only the v1 snapshot still references it.
    _save_greeting(client, pid, "Draft no longer uses it.")
    body = client.get(f"/v1/admin/custom-variables/{vid}/references").json()
    entries = {p["id"]: p for p in body["profiles"]}
    assert pid in entries
    assert any("v1" in w and "greeting" in w for w in entries[pid]["where"])


def test_reference_match_is_exact_not_substring(client, admin_session):
    # "state" must NOT match "{{state_full}}" (exact token, not substring).
    vid = _make_var(client, "state")
    pid = _new_profile(client)
    _save_greeting(client, pid, "Your {{state_full}} is set.")
    body = client.get(f"/v1/admin/custom-variables/{vid}/references").json()
    assert body["profiles"] == []


def test_references_leak_no_prompt_text(client, admin_session):
    # Names/locations only — the response must never echo the prompt body.
    vid = _make_var(client, "promo")
    pid = _new_profile(client)
    secret = "Hello, special {{promo}} for Margaret!"
    _save_greeting(client, pid, secret)
    raw = client.get(f"/v1/admin/custom-variables/{vid}/references").text
    assert "Margaret" not in raw
    assert "special" not in raw
