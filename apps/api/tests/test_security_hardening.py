"""Unit tests locking in the security-review hardening (no DB / no app needed).

One assertion-cluster per finding:
- mass-assignment guard on ContactUpdate (extra="forbid")
- lenient inbound caller-ID validation (rejects garbage, keeps bare national + E.164)
- dynamic_vars scalar guard (rejects nested objects/arrays)
- prompt sanitizer now strips U+2028 / U+2029 (parity with the agent + SMS sanitizers)
- non-reversible email fingerprint for auth logs
- X-Forwarded-For trusted-proxy enforcement in client_ip
- the dedicated /v1/auth/* rate-limit bucket
- the per-call tool-plane rate ceiling
"""

import uuid

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from usan_api.client_ip import client_ip
from usan_api.masking import email_fingerprint
from usan_api.prompt_substitution import sanitize_prompt_value
from usan_api.ratelimit import OperatorRateLimitMiddleware, tool_call_within_limit
from usan_api.schemas.call import CreateCallRequest, InboundCallRequest
from usan_api.schemas.contact import ContactUpdate

# --- mass-assignment guard ----------------------------------------------------


def test_contact_update_forbids_unknown_fields():
    ContactUpdate(name="Ada")  # known field is fine
    with pytest.raises(ValidationError):
        ContactUpdate(name="Ada", org_id=str(uuid.uuid4()))  # privileged extra -> 422
    with pytest.raises(ValidationError):
        ContactUpdate(agent_profile_id=str(uuid.uuid4()))


# --- lenient inbound caller-ID validation -------------------------------------


def test_inbound_phone_allows_bare_national_and_e164():
    assert InboundCallRequest(phone_e164="6692388604", livekit_room="r").phone_e164 == "6692388604"
    assert (
        InboundCallRequest(phone_e164="+16692388604", livekit_room="r").phone_e164 == "+16692388604"
    )
    # SIP-style caller-IDs must still pass (to_e164 strips them down later).
    assert InboundCallRequest(phone_e164="sip:+16692388604@x", livekit_room="r").phone_e164


def test_inbound_phone_blank_becomes_none():
    assert InboundCallRequest(phone_e164="   ", livekit_room="r").phone_e164 is None
    assert InboundCallRequest(phone_e164=None, livekit_room="r").phone_e164 is None


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\x00b", "a\x7fb", "x" * 40])
def test_inbound_phone_rejects_control_chars_and_overlength(bad):
    # Only control characters (the log-forging vector) and the pre-existing 32-char cap
    # are rejected; printable metacharacters are inert (normalized away by to_e164).
    with pytest.raises(ValidationError):
        InboundCallRequest(phone_e164=bad, livekit_room="r")


@pytest.mark.parametrize("caller", ["<+16692388604>", "+16692388604;tag=abc", "sip:+16692388604@h"])
def test_inbound_phone_accepts_sip_from_header_forms(caller):
    # SIP From-header punctuation (<, >, ", ;tag=) must pass: to_e164 strips it to recover
    # the number; rejecting it would silently de-personalize a known caller (review fix).
    assert InboundCallRequest(phone_e164=caller, livekit_room="r").phone_e164 == caller


# --- dynamic_vars scalar guard ------------------------------------------------


def test_dynamic_vars_allows_scalars():
    req = CreateCallRequest(
        contact_id=uuid.uuid4(),
        idempotency_key="k1",
        dynamic_vars={"s": "x", "n": 1, "b": True, "z": None},
    )
    assert req.dynamic_vars["n"] == 1


@pytest.mark.parametrize("nested", [{"a": {"k": "v"}}, {"a": [1, 2, 3]}])
def test_dynamic_vars_rejects_nested(nested):
    with pytest.raises(ValidationError):
        CreateCallRequest(contact_id=uuid.uuid4(), idempotency_key="k2", dynamic_vars=nested)


# --- prompt sanitizer covers the Unicode line/paragraph separators ------------


def test_prompt_sanitizer_strips_line_and_paragraph_separators():
    raw = "alpha\u2028beta\u2029gamma"
    out = sanitize_prompt_value(raw, max_len=100)
    assert "\u2028" not in out
    assert "\u2029" not in out
    assert "alpha" in out
    assert "beta" in out
    assert "gamma" in out


# --- email fingerprint --------------------------------------------------------


def test_email_fingerprint_is_stable_caseless_and_opaque():
    assert email_fingerprint(None) == "unknown"
    assert email_fingerprint("") == "unknown"
    fp = email_fingerprint("  Ada@Example.COM ")
    assert fp == email_fingerprint("ada@example.com")  # case + whitespace insensitive
    assert len(fp) == 12
    assert "@" not in fp  # not the raw address
    assert "ada" not in fp


# --- X-Forwarded-For trusted-proxy enforcement --------------------------------


def _req(xff: str | None, peer: str | None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff is not None else []
    scope = {
        "type": "http",
        "headers": headers,
        "client": (peer, 12345) if peer is not None else None,
    }
    return Request(scope)


def test_client_ip_legacy_trusts_xff_when_no_trusted_set():
    # Empty trusted set (the default) preserves the existing behavior.
    assert client_ip(_req("1.2.3.4", "10.0.0.9")) == "1.2.3.4"


def test_client_ip_trusts_xff_only_from_a_trusted_peer():
    trusted = frozenset({"10.0.0.9"})
    # Peer IS the configured proxy -> honor the forwarded client.
    assert client_ip(_req("1.2.3.4", "10.0.0.9"), trusted) == "1.2.3.4"
    # Peer is NOT trusted -> the forwarded header is a spoof; use the real socket peer.
    assert client_ip(_req("1.2.3.4", "203.0.113.7"), trusted) == "203.0.113.7"


# --- rate-limit buckets -------------------------------------------------------


async def _noop_app(scope, receive, send):  # pragma: no cover - never invoked here
    return None


def test_auth_routes_use_a_separate_tighter_bucket():
    mw = OperatorRateLimitMiddleware(
        _noop_app, limit="60/minute", enabled=True, auth_limit="10/minute"
    )
    auth_limit, auth_ns = mw._limit_for("/v1/auth/callback")
    op_limit, op_ns = mw._limit_for("/v1/contacts")
    assert (auth_ns, op_ns) == ("auth", "operator")
    assert auth_limit.amount == 10
    assert op_limit.amount == 60


def test_tool_call_within_limit_trips_after_budget():
    call_id = f"unit-{uuid.uuid4()}"  # unique key so module-level state can't collide
    assert tool_call_within_limit(call_id, "3/minute")
    assert tool_call_within_limit(call_id, "3/minute")
    assert tool_call_within_limit(call_id, "3/minute")
    assert not tool_call_within_limit(call_id, "3/minute")  # 4th over the window
    # A different call gets its own independent budget.
    assert tool_call_within_limit(f"unit-{uuid.uuid4()}", "3/minute")
