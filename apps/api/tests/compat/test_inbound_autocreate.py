"""Unknown-recipient inbound SMS auto-create (Phase 4b-3)."""

from __future__ import annotations

from usan_api.compat.inbound_autocreate import _pick_inbound_sms_agent
from usan_api.db.models import PhoneNumber


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
