"""Frozen: DELETE /delete-agent-version/{agent_id}?version=N — guard published version.

Guards:
- 409 when trying to delete the currently-published version
- 404 for an unknown version number
- 422 when the required ?version query param is missing
"""

import pytest

from tests.compat.conftest import _published_agent_id

pytestmark = pytest.mark.frozen


def test_delete_published_version_is_409(compat_client, compat_headers):
    agent_id = _published_agent_id(compat_client, compat_headers)
    # Fetch the current published version number from get-agent; _published_agent_id
    # calls create-agent (v1) then publish-agent-version (v2), so published_version is 2.
    published_version = compat_client.get(f"/get-agent/{agent_id}", headers=compat_headers).json()[
        "version"
    ]
    resp = compat_client.delete(
        f"/delete-agent-version/{agent_id}",
        params={"version": published_version},
        headers=compat_headers,
    )
    assert resp.status_code == 409, resp.text


def test_delete_unknown_version_is_404(compat_client, compat_headers):
    agent_id = _published_agent_id(compat_client, compat_headers)
    resp = compat_client.delete(
        f"/delete-agent-version/{agent_id}", params={"version": 999}, headers=compat_headers
    )
    assert resp.status_code == 404, resp.text


def test_missing_version_query_is_422(compat_client, compat_headers):
    agent_id = _published_agent_id(compat_client, compat_headers)
    assert (
        compat_client.delete(
            f"/delete-agent-version/{agent_id}", headers=compat_headers
        ).status_code
        == 422  # version is a required query param
    )
