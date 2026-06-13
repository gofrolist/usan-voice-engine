"""Optimistic concurrency for profile drafts (FR-032 / SC-011).

A save against a draft that changed since it was loaded is rejected with 409 so
no edits are silently overwritten. The token is a monotonic ``draft_revision``
integer bumped by every row-mutating path (update_draft / publish / rollback).
Written FIRST (Constitution IV): asserts behavior that does not exist yet.
"""

import uuid


def _name() -> str:
    return f"profile-{uuid.uuid4().hex}"


def _new_profile(client) -> str:
    return client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]


def _valid_config(client) -> dict:
    # Borrow a real default config so the body passes AgentConfig validation; the
    # 404 path is reached only after the body validates.
    pid = _new_profile(client)
    return client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]


def test_detail_exposes_draft_revision(client, admin_session):
    pid = _new_profile(client)
    detail = client.get(f"/v1/admin/profiles/{pid}").json()
    # A fresh profile starts at revision 1 (migration default; no backfill).
    assert detail["draft_revision"] == 1


def test_summary_exposes_draft_revision(client, admin_session):
    _new_profile(client)
    rows = client.get("/v1/admin/profiles").json()
    assert all("draft_revision" in r for r in rows)


def test_save_with_matching_revision_advances_it(client, admin_session):
    pid = _new_profile(client)
    detail = client.get(f"/v1/admin/profiles/{pid}").json()
    rev = detail["draft_revision"]
    cfg = detail["draft_config"]
    cfg["prompts"]["greeting"] = "Hi there, checking in."
    r = client.put(
        f"/v1/admin/profiles/{pid}/draft",
        json={"config": cfg, "expected_revision": rev},
    )
    assert r.status_code == 200
    assert r.json()["draft_revision"] == rev + 1


def test_concurrent_double_save_returns_409(client, admin_session):
    pid = _new_profile(client)
    detail = client.get(f"/v1/admin/profiles/{pid}").json()
    stale_rev = detail["draft_revision"]
    cfg = detail["draft_config"]
    # Session A saves successfully against the revision both sessions loaded.
    cfg["prompts"]["greeting"] = "Edit from session A."
    first = client.put(
        f"/v1/admin/profiles/{pid}/draft",
        json={"config": cfg, "expected_revision": stale_rev},
    )
    assert first.status_code == 200
    # Session B still holds the now-stale revision: its save is blocked.
    cfg["prompts"]["greeting"] = "Edit from session B."
    second = client.put(
        f"/v1/admin/profiles/{pid}/draft",
        json={"config": cfg, "expected_revision": stale_rev},
    )
    assert second.status_code == 409
    # The conflict message must carry no PHI / no other actor's identity.
    detail_msg = second.json()["detail"].lower()
    assert "reload" in detail_msg
    assert "session a" not in detail_msg


def test_reload_then_save_succeeds(client, admin_session):
    pid = _new_profile(client)
    first_detail = client.get(f"/v1/admin/profiles/{pid}").json()
    rev = first_detail["draft_revision"]
    cfg = first_detail["draft_config"]
    cfg["prompts"]["greeting"] = "First save."
    assert (
        client.put(
            f"/v1/admin/profiles/{pid}/draft",
            json={"config": cfg, "expected_revision": rev},
        ).status_code
        == 200
    )
    # The losing session reloads (re-fetches the latest revision), re-applies, saves.
    fresh = client.get(f"/v1/admin/profiles/{pid}").json()
    fresh_cfg = fresh["draft_config"]
    fresh_cfg["prompts"]["greeting"] = "Re-applied after reload."
    r = client.put(
        f"/v1/admin/profiles/{pid}/draft",
        json={"config": fresh_cfg, "expected_revision": fresh["draft_revision"]},
    )
    assert r.status_code == 200


def test_omitted_expected_revision_is_unconditional(client, admin_session):
    # Backward compatibility: a body without expected_revision always saves.
    pid = _new_profile(client)
    cfg = client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]
    cfg["prompts"]["greeting"] = "Unconditional save."
    r = client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    assert r.status_code == 200


def test_publish_advances_draft_revision(client, admin_session):
    pid = _new_profile(client)
    before = client.get(f"/v1/admin/profiles/{pid}").json()["draft_revision"]
    assert client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"}).status_code == 201
    after = client.get(f"/v1/admin/profiles/{pid}").json()["draft_revision"]
    assert after > before


def test_rollback_advances_draft_revision(client, admin_session):
    pid = _new_profile(client)
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"})
    cfg = client.get(f"/v1/admin/profiles/{pid}").json()["draft_config"]
    cfg["prompts"]["greeting"] = "Changed before v2."
    client.put(f"/v1/admin/profiles/{pid}/draft", json={"config": cfg})
    client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v2"})
    before = client.get(f"/v1/admin/profiles/{pid}").json()["draft_revision"]
    assert client.post(f"/v1/admin/profiles/{pid}/rollback/1", json={}).status_code == 201
    after = client.get(f"/v1/admin/profiles/{pid}").json()["draft_revision"]
    assert after > before


def test_stale_save_on_missing_profile_returns_404_not_409(client, admin_session):
    # A guarded UPDATE that matches 0 rows must re-SELECT to disambiguate: the row
    # is absent here, so the answer is 404 (not found), never 409 (stale).
    r = client.put(
        f"/v1/admin/profiles/{uuid.uuid4()}/draft",
        json={"config": _valid_config(client), "expected_revision": 1},
    )
    assert r.status_code == 404
