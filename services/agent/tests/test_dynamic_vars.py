"""Unit tests for the agent-side dynamic-variable receiver.

Tests cover the pure ``apply_dynamic_vars`` function only — no LiveKit room spin-up.
The wire-contract constant is frozen here so a rename on either side causes a test failure.
"""

from __future__ import annotations

import json

from usan_agent.dynamic_vars import DYNAMIC_VARS_TOPIC, apply_dynamic_vars


def test_apply_dynamic_vars_resubstitutes_and_updates() -> None:
    captured: dict[str, str] = {}
    apply_dynamic_vars(
        json.dumps({"first_name": "Ada"}).encode(),
        current_vars={"first_name": "there"},
        template="Hello {{first_name}}, time for your check-in.",
        on_update=lambda text: captured.setdefault("text", text),
    )
    assert captured["text"] == "Hello Ada, time for your check-in."


def test_apply_dynamic_vars_ignores_blank_payload() -> None:
    captured: dict[str, str] = {}
    apply_dynamic_vars(
        b"",
        current_vars={},
        template="x",
        on_update=lambda t: captured.setdefault("t", t),
    )
    assert "t" not in captured


def test_apply_dynamic_vars_ignores_whitespace_only_payload() -> None:
    captured: dict[str, str] = {}
    apply_dynamic_vars(
        b"   ",
        current_vars={},
        template="x",
        on_update=lambda t: captured.setdefault("t", t),
    )
    assert "t" not in captured


def test_apply_dynamic_vars_malformed_json_does_not_raise_and_does_not_update() -> None:
    captured: dict[str, str] = {}
    # Must not raise; must not call on_update.
    apply_dynamic_vars(
        b"{not valid json",
        current_vars={"first_name": "there"},
        template="Hello {{first_name}}",
        on_update=lambda t: captured.setdefault("t", t),
    )
    assert "t" not in captured


def test_apply_dynamic_vars_non_dict_payload_does_not_update() -> None:
    captured: dict[str, str] = {}
    apply_dynamic_vars(
        json.dumps(["a", "b"]).encode(),
        current_vars={},
        template="x",
        on_update=lambda t: captured.setdefault("t", t),
    )
    assert "t" not in captured


def test_apply_dynamic_vars_empty_dict_does_not_update() -> None:
    captured: dict[str, str] = {}
    apply_dynamic_vars(
        json.dumps({}).encode(),
        current_vars={},
        template="x",
        on_update=lambda t: captured.setdefault("t", t),
    )
    assert "t" not in captured


def test_apply_dynamic_vars_merges_with_existing_vars() -> None:
    """Incoming vars override current; non-overridden current vars are preserved."""
    captured: dict[str, str] = {}
    apply_dynamic_vars(
        json.dumps({"first_name": "Ada"}).encode(),
        current_vars={"first_name": "there", "contact_name": "Smith"},
        template="Hello {{first_name}} {{contact_name}}",
        on_update=lambda t: captured.setdefault("t", t),
    )
    assert captured["t"] == "Hello Ada Smith"


def test_apply_dynamic_vars_coerces_values_to_str() -> None:
    """Numeric values in the JSON are cast to str before substitution."""
    captured: dict[str, str] = {}
    apply_dynamic_vars(
        json.dumps({"first_name": 42}).encode(),
        current_vars={},
        template="Hello {{first_name}}",
        on_update=lambda t: captured.setdefault("t", t),
    )
    assert captured["t"] == "Hello 42"


def test_topic_matches_api_side() -> None:
    """Wire-contract freeze: DYNAMIC_VARS_TOPIC must equal the api-side literal."""
    assert DYNAMIC_VARS_TOPIC == "usan/vars"
