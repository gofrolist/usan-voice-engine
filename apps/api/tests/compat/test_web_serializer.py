"""serialize_call web branch: web_call type, minted token, phone fields omitted."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from usan_api.compat import call_serializer
from usan_api.compat.serialization import pack_unhonored, unpack_dynamic_vars
from usan_api.db.base import CallDirection, CallStatus, CallType, ProfileStatus
from usan_api.db.models import AgentProfile, Call
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context


def test_unpack_strips_unhonored_blob() -> None:
    packed = pack_unhonored(
        {"k": "v", "__meta__": '{"name": "Pat"}'},
        agent_override={"voice_id": "x"},
        current_node_id="n1",
        current_state=None,
    )
    dynamic_vars, metadata = unpack_dynamic_vars(packed)
    assert dynamic_vars == {"k": "v"}  # bare user vars only
    assert metadata == {"name": "Pat"}  # echoed metadata pristine
    assert "__meta_unhonored__" not in dynamic_vars
    assert "__meta_unhonored__" not in metadata


@pytest.mark.asyncio
async def test_serialize_web_call_shape(app_session) -> None:
    # Resolve org and set tenant context (calls table is RLS-scoped).
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)

    # Seed a published AgentProfile inline (same pattern as test_contacts_repo.py).
    profile = AgentProfile(
        name="Web Test Agent",
        draft_config={"general_prompt": "hi"},
        status=ProfileStatus.ACTIVE,
        published_version=1,
    )
    app_session.add(profile)
    await app_session.flush()

    call = Call(
        direction=CallDirection.INBOUND,
        status=CallStatus.REGISTERED,
        call_type=CallType.WEB_CALL,
        profile_override=profile.id,
        livekit_room="usan-web-abc123",
        dynamic_vars={"greeting": "hi"},
    )
    app_session.add(call)
    await app_session.flush()

    settings = get_settings()
    out = await call_serializer.serialize_call(app_session, call, settings, client_host="1.2.3.4")
    dumped = out.model_dump(exclude_none=True)
    assert dumped["call_type"] == "web_call"
    assert isinstance(dumped["access_token"], str)
    assert dumped["access_token"]
    assert dumped["call_status"] == "registered"
    assert isinstance(dumped["agent_version"], int)
    for phone_field in ("from_number", "to_number", "direction", "telephony_identifier"):
        assert phone_field not in dumped
