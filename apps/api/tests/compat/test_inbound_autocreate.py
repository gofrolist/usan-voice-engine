"""Unknown-recipient inbound SMS auto-create (Phase 4b-3).

Gate tests only: create+reply+commit and redelivery dedup are validated at the webhook level
in tests/test_inbound_sms_autocreate_webhook.py (added in Task 4), mirroring the 4b-2
reply-engine test split.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from usan_api import telnyx_messaging
from usan_api.compat import ids
from usan_api.compat.inbound_autocreate import _pick_inbound_sms_agent, handle_inbound_autocreate
from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, PhoneNumber
from usan_api.repositories import family_contacts as family_contacts_repo
from usan_api.schemas.inbound_sms import InboundSms
from usan_api.settings import get_settings
from usan_api.tenant_context import set_tenant_context

# ---------------------------------------------------------------------------
# Task-2 unit tests: _pick_inbound_sms_agent
# ---------------------------------------------------------------------------


def _pn(inbound_sms_agents):
    return PhoneNumber(
        phone_e164="+15550000000",
        phone_number_type="custom",
        inbound_sms_agents=inbound_sms_agents,
    )


def test_pick_first_entry():
    pn = _pn([{"agent_id": "agent_aaa", "weight": 1.0}, {"agent_id": "agent_bbb"}])
    assert _pick_inbound_sms_agent(pn) == "agent_aaa"


def test_pick_none_phone_number():
    assert _pick_inbound_sms_agent(None) is None


def test_pick_empty_binding():
    assert _pick_inbound_sms_agent(_pn(None)) is None
    assert _pick_inbound_sms_agent(_pn([])) is None


def test_pick_malformed_entry():
    assert _pick_inbound_sms_agent(_pn([{"weight": 1.0}])) is None  # no agent_id
    assert _pick_inbound_sms_agent(_pn([{"agent_id": ""}])) is None  # blank
    assert _pick_inbound_sms_agent(_pn([{"agent_id": 123}])) is None  # non-str


# ---------------------------------------------------------------------------
# Gate tests (Task 3): these return before any commit, so they are valid on
# app_session (which uses rollback-based isolation).
# ---------------------------------------------------------------------------

_OUR = "+15550000000"
_SENDER = "+15551234567"


def _settings(**overrides):
    base = {
        "telnyx_inbound_sms_autocreate_enabled": True,
        "telnyx_messaging_enabled": True,
        "telnyx_messaging_api_key": SecretStr("k"),
        "telnyx_messaging_profile_id": "p",
        "telnyx_from_number": _OUR,
        "gcp_project": "proj",
    }
    base.update(overrides)
    return get_settings().model_copy(update=base)


def _inbound(message_id="m1", *, sender=_SENDER, recipient=_OUR, text_body="hello"):
    return InboundSms(
        message_id=message_id,
        from_number=sender,
        to_number=recipient,
        text=text_body,
        event_type="message.received",
    )


async def _seed(db, *, bind=True, active=True):
    """Set tenant context, seed a (live) agent profile + a phone_number bound to it."""
    org_id = (await db.execute(text("SELECT id FROM organizations LIMIT 1"))).scalar_one()
    await set_tenant_context(db, org_id)
    profile = AgentProfile(
        name=f"A {uuid.uuid4().hex[:8]}",
        draft_config={"general_prompt": "x"},
        status=ProfileStatus.ACTIVE if active else ProfileStatus.ARCHIVED,
        published_version=1 if active else None,
    )
    db.add(profile)
    await db.flush()
    agents = [{"agent_id": ids.encode_agent_id(profile.id), "weight": 1.0}] if bind else None
    db.add(PhoneNumber(phone_e164=_OUR, phone_number_type="custom", inbound_sms_agents=agents))
    await db.flush()
    return profile


@pytest.fixture
def recorded_sms(monkeypatch):
    calls: list[dict[str, str]] = []

    async def _send(settings, *, to_number, body):
        calls.append({"to_number": to_number, "body": body})
        return "tx-out"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _send)
    return calls


@pytest.mark.asyncio
async def test_flag_off_is_noop(app_session, recorded_sms):
    await _seed(app_session)
    result = await handle_inbound_autocreate(
        app_session, _settings(telnyx_inbound_sms_autocreate_enabled=False), _inbound()
    )
    assert result is False
    assert recorded_sms == []
    await app_session.rollback()


@pytest.mark.asyncio
async def test_no_binding_declines(app_session, recorded_sms):
    await _seed(app_session, bind=False)  # phone_number exists but no inbound_sms_agents
    result = await handle_inbound_autocreate(app_session, _settings(), _inbound())
    assert result is False
    assert recorded_sms == []
    await app_session.rollback()


@pytest.mark.asyncio
async def test_non_live_bound_agent_declines(app_session, recorded_sms):
    await _seed(app_session, active=False)  # bound, but agent archived/unpublished
    result = await handle_inbound_autocreate(app_session, _settings(), _inbound())
    assert result is False
    await app_session.rollback()


@pytest.mark.asyncio
async def test_known_family_contact_declines(app_session, recorded_sms, monkeypatch):
    await _seed(app_session)

    async def _match(db, phone):
        return [object()]  # a known family contact for this sender

    monkeypatch.setattr(family_contacts_repo, "find_contacts_by_phone", _match)
    result = await handle_inbound_autocreate(app_session, _settings(), _inbound())
    assert result is False  # caregiver relay not hijacked -> family-task runs
    await app_session.rollback()


@pytest.mark.asyncio
async def test_unconfigured_owns_but_skips(app_session, recorded_sms):
    await _seed(app_session)
    result = await handle_inbound_autocreate(app_session, _settings(gcp_project=None), _inbound())
    assert result is True  # bound DID is SMS-agent territory -> owned, not relayed
    assert recorded_sms == []
    await app_session.rollback()


@pytest.mark.asyncio
async def test_open_chat_declines(app_session, recorded_sms):
    from usan_api.db.base import ChatStatus
    from usan_api.db.models import ChatSession

    profile = await _seed(app_session)  # bound DID + live agent
    # A continuing conversation: an open sms_chat already exists for this our-DID / sender pair.
    app_session.add(
        ChatSession(
            agent_profile_id=profile.id,
            agent_version=1,
            chat_type="sms_chat",
            dynamic_vars={},
            from_number=_OUR,
            to_number=_SENDER,
            status=ChatStatus.ONGOING,
        )
    )
    await app_session.flush()
    result = await handle_inbound_autocreate(app_session, _settings(), _inbound())
    assert result is False  # belongs to the reply engine, not auto-create
    assert recorded_sms == []
    count = (
        await app_session.execute(
            text("SELECT count(*) FROM chat_sessions WHERE to_number = :s"), {"s": _SENDER}
        )
    ).scalar_one()
    assert count == 1  # no SECOND chat created
    await app_session.rollback()
