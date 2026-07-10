"""create_web_call service: agent gate, REGISTERED web row, dispatch, audit-stash, 502."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from usan_api import livekit_dispatch
from usan_api.compat import call_create
from usan_api.compat.errors import CompatError
from usan_api.compat.ids import encode_agent_id
from usan_api.compat.schemas.calls import CreateWebCallRequest
from usan_api.compat.serialization import unpack_dynamic_vars
from usan_api.db.base import CallStatus, CallType, ProfileStatus
from usan_api.db.models import AgentProfile
from usan_api.settings import Settings
from usan_api.tenant_context import set_tenant_context


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


async def _seed_published_profile(app_session) -> AgentProfile:
    """Seed a PUBLISHED AgentProfile (ACTIVE + published_version set) in the current tenant."""
    profile = AgentProfile(
        name=f"Web Call Agent {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "hello"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    app_session.add(profile)
    await app_session.flush()
    return profile


@pytest.mark.asyncio
async def test_create_web_call_persists_registered_web_row(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)

    published_profile = await _seed_published_profile(app_session)
    settings = _settings()

    body = CreateWebCallRequest(
        agent_id=encode_agent_id(published_profile.id),
        metadata={"external_id": "e1"},
        retell_llm_dynamic_variables={"name": "Pat"},
        agent_override={"voice_id": "v"},
    )
    with patch.object(livekit_dispatch, "dispatch_web_agent", new=AsyncMock()) as disp:
        call = await call_create.create_web_call(app_session, settings, body)

    assert call.call_type is CallType.WEB_CALL
    assert call.status is CallStatus.REGISTERED
    assert call.livekit_room is not None
    assert call.livekit_room.startswith("usan-web-")
    # un-honored audit blob persisted but NOT echoed
    dynamic_vars, metadata = unpack_dynamic_vars(call.dynamic_vars)
    assert dynamic_vars == {"name": "Pat"}
    assert metadata == {"external_id": "e1"}
    assert "__meta_unhonored__" in call.dynamic_vars
    # dispatch received only the bare user vars
    disp.assert_awaited_once()
    assert disp.await_args.kwargs["dynamic_vars"] == {"name": "Pat"}


@pytest.mark.asyncio
async def test_create_web_call_dispatch_failure_rolls_back(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)

    published_profile = await _seed_published_profile(app_session)
    settings = _settings()

    body = CreateWebCallRequest(agent_id=encode_agent_id(published_profile.id))
    with (
        patch.object(
            livekit_dispatch, "dispatch_web_agent", new=AsyncMock(side_effect=RuntimeError("boom"))
        ),
        pytest.raises(CompatError) as exc,
    ):
        await call_create.create_web_call(app_session, settings, body)
    assert exc.value.status_code == 502
    assert "boom" not in str(exc.value.message)  # internal detail never surfaced
