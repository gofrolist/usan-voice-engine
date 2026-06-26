"""dispatch_web_agent: web dispatch metadata carries session_kind=call + call_type=web_call,
creates the room, and does NOT require outbound (SIP) configuration."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from usan_api import livekit_dispatch
from usan_api.settings import Settings


def _settings(**overrides) -> Settings:
    base = {
        "DATABASE_URL": "postgresql://u:p@host/db",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "JWT_SIGNING_KEY": "s" * 32,
        "OPERATOR_API_KEY": "o" * 32,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.mark.asyncio
async def test_dispatch_web_agent_metadata_and_room() -> None:
    settings = _settings()
    fake_api = MagicMock()
    fake_api.__aenter__ = AsyncMock(return_value=fake_api)
    fake_api.__aexit__ = AsyncMock(return_value=False)
    fake_api.room.create_room = AsyncMock()
    fake_api.agent_dispatch.create_dispatch = AsyncMock()

    with patch.object(livekit_dispatch, "build_livekit_api", return_value=fake_api):
        await livekit_dispatch.dispatch_web_agent(
            settings=settings,
            room="usan-web-xyz",
            call_id="cid-1",
            dynamic_vars={"name": "Pat"},
            resolved_vars={},
            timezone="America/New_York",
        )

    fake_api.room.create_room.assert_awaited_once()
    dispatch_arg = fake_api.agent_dispatch.create_dispatch.await_args.args[0]
    meta = json.loads(dispatch_arg.metadata)
    assert meta["session_kind"] == "call"
    assert meta["call_type"] == "web_call"
    assert meta["call_id"] == "cid-1"
    assert meta["dynamic_vars"] == {"name": "Pat"}
    assert meta["resolved_vars"] == {}
