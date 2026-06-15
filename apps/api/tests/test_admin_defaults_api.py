"""Tests for GET /v1/admin/defaults (US3 / FR-016..020, T038).

The defaults endpoint is read-only and PHI-free: it states, per direction, which
profile is the current default and whether it is still effective (eligible =
ACTIVE + published), exposes the built-in DEFAULT_AGENT_CONFIG read-only, and
returns a plain-language resolution-order descriptor. Names/non-PHI only.
"""

import uuid

from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG


def _name() -> str:
    return f"profile-{uuid.uuid4().hex}"


def _publish_new_profile(client) -> str:
    """Create + publish a profile so it is eligible to be a default."""
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    assert client.post(f"/v1/admin/profiles/{pid}/publish", json={"note": "v1"}).status_code == 201
    return pid


def test_defaults_requires_session(client):
    # The management plane rejects an unauthenticated request.
    assert client.get("/v1/admin/defaults").status_code == 401


def test_defaults_no_defaults_set_reports_null(client, admin_session):
    r = client.get("/v1/admin/defaults")
    assert r.status_code == 200
    body = r.json()
    by_dir = {d["direction"]: d for d in body["directions"]}
    assert set(by_dir) == {"inbound", "outbound"}
    assert by_dir["inbound"]["default_profile"] is None
    assert by_dir["outbound"]["default_profile"] is None
    assert by_dir["inbound"]["ineligible"] is False
    assert by_dir["outbound"]["ineligible"] is False


def test_defaults_returns_builtin_fallback_readonly(client, admin_session):
    body = client.get("/v1/admin/defaults").json()
    # The built-in last-resort fallback is the server DEFAULT_AGENT_CONFIG verbatim.
    assert body["builtin_fallback"] == DEFAULT_AGENT_CONFIG.model_dump()


def test_defaults_resolution_order_is_four_tiers(client, admin_session):
    body = client.get("/v1/admin/defaults").json()
    order = body["resolution_order"]
    assert len(order) == 4
    # Plain-language descriptor in precedence order: override -> contact -> default -> fallback.
    joined = " ".join(order).lower()
    assert "override" in joined
    assert "contact" in joined
    assert "default" in joined
    assert "fallback" in joined or "built-in" in joined


def _set_default(client, pid: str, direction: str) -> None:
    r = client.post(f"/v1/admin/profiles/{pid}/set-default", json={"direction": direction})
    assert r.status_code == 200


def test_defaults_reports_current_default_name_and_eligible(client, admin_session):
    pid = _publish_new_profile(client)
    _set_default(client, pid, "inbound")
    body = client.get("/v1/admin/defaults").json()
    by_dir = {d["direction"]: d for d in body["directions"]}
    dp = by_dir["inbound"]["default_profile"]
    assert dp is not None
    assert dp["id"] == pid
    assert dp["eligible"] is True
    assert by_dir["inbound"]["ineligible"] is False
    # outbound still unset
    assert by_dir["outbound"]["default_profile"] is None


def test_defaults_unpublished_default_is_ineligible(client, admin_session):
    # A profile can be set as default while it has a draft but no published version.
    pid = client.post("/v1/admin/profiles", json={"name": _name()}).json()["id"]
    _set_default(client, pid, "outbound")
    body = client.get("/v1/admin/defaults").json()
    by_dir = {d["direction"]: d for d in body["directions"]}
    dp = by_dir["outbound"]["default_profile"]
    assert dp is not None
    assert dp["id"] == pid
    assert dp["eligible"] is False
    # The page must surface that this default is no longer effective (FR-020).
    assert by_dir["outbound"]["ineligible"] is True
    assert by_dir["outbound"]["ineligible_reason"] == "unpublished"


def test_defaults_never_returns_phi_or_call_keys(client, admin_session):
    # The response is a fixed read model: per-direction default refs + resolution
    # order + the built-in config. It must carry NO per-call PHI key (a contact's
    # masked phone, contact id, transcript, etc.). The static built-in prompt copy may
    # contain the word "phone" ("...an contact over the phone..."), so assert on the
    # response KEYS, not a substring of the rendered prompt text.
    pid = _publish_new_profile(client)
    client.post(f"/v1/admin/profiles/{pid}/set-default", json={"direction": "inbound"})
    body = client.get("/v1/admin/defaults").json()
    assert set(body) == {"directions", "resolution_order", "builtin_fallback"}
    dp = next(d for d in body["directions"] if d["direction"] == "inbound")["default_profile"]
    # Only name/id/status/version/eligibility — never a phone, masked_phone or contact_id.
    assert set(dp) == {"id", "name", "status", "published_version", "eligible"}
