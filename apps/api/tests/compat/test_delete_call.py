import pytest

from tests.compat.conftest import _published_agent_id, create_call

pytestmark = pytest.mark.frozen


def test_delete_call_archives_and_redacts(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    agent_id = _published_agent_id(compat_client, compat_headers)
    resp_create = create_call(compat_client, compat_headers, override_agent_id=agent_id)
    call_id = resp_create.json()["call_id"]

    resp = compat_client.delete(f"/v2/delete-call/{call_id}", headers=compat_headers)
    assert resp.status_code == 204, resp.text

    # Archived -> get-call 404
    assert compat_client.get(f"/v2/get-call/{call_id}", headers=compat_headers).status_code == 404

    # Archived -> excluded from list-calls (envelope key is "items")
    listed = compat_client.post("/v3/list-calls", json={}, headers=compat_headers).json()
    assert all(c["call_id"] != call_id for c in listed.get("items", []))


def test_delete_unknown_call_is_404(compat_client, compat_headers):
    resp = compat_client.delete("/v2/delete-call/call_doesnotexist", headers=compat_headers)
    assert resp.status_code == 404
