"""Call.call_type discriminator: enum values + phone-default backfill."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from usan_api.db.base import CallDirection, CallStatus, CallType
from usan_api.db.models import Call
from usan_api.tenant_context import set_tenant_context


def test_call_type_enum_values() -> None:
    assert CallType.PHONE_CALL.value == "phone_call"
    assert CallType.WEB_CALL.value == "web_call"


@pytest.mark.asyncio
async def test_new_call_defaults_to_phone_call(app_session) -> None:
    # A Call created WITHOUT call_type takes the server_default 'phone_call'.
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    call = Call(direction=CallDirection.OUTBOUND, status=CallStatus.QUEUED)
    app_session.add(call)
    await app_session.flush()
    await app_session.refresh(call)
    assert call.call_type is CallType.PHONE_CALL


@pytest.mark.asyncio
async def test_web_call_type_round_trips(app_session) -> None:
    org_id = (await app_session.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(app_session, org_id)
    call = Call(
        direction=CallDirection.INBOUND, status=CallStatus.REGISTERED, call_type=CallType.WEB_CALL
    )
    app_session.add(call)
    await app_session.flush()
    await app_session.refresh(call)
    assert call.call_type is CallType.WEB_CALL
