import pytest
from pydantic import ValidationError

from usan_api.settings import Settings

_BASE = {
    "DATABASE_URL": "postgresql://u:p@localhost/db",
    "LIVEKIT_API_KEY": "k",
    "LIVEKIT_API_SECRET": "a" * 32,
    "LIVEKIT_URL": "ws://livekit:7880",
    "JWT_SIGNING_KEY": "s" * 32,
    "OPERATOR_API_KEY": "o" * 32,
}


def test_external_tools_default_inert():
    # Ship-inert: with defaults the feature is off and no allow-list is required.
    s = Settings(**_BASE)
    assert s.compat_external_tools_enabled is False
    assert s.compat_tool_allowed_hosts == ""


def test_external_tools_enabled_requires_allowlist():
    # Fail-closed (Surface 3 WS-C): enabling with an empty allow-list would 422 every ingest
    # and 502 every call — a silently-broken feature. Fail at startup instead.
    with pytest.raises(ValidationError, match="COMPAT_TOOL_ALLOWED_HOSTS"):
        Settings(**_BASE, COMPAT_EXTERNAL_TOOLS_ENABLED="true")


def test_external_tools_enabled_with_allowlist_ok():
    s = Settings(
        **_BASE,
        COMPAT_EXTERNAL_TOOLS_ENABLED="true",
        COMPAT_TOOL_ALLOWED_HOSTS="client.example.com",
    )
    assert s.compat_external_tools_enabled is True
    assert "client.example.com" in s.compat_tool_allowed_hosts
