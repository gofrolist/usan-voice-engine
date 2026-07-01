"""Unit tests for the agent-side dynamic-variable receiver.

Tests cover the pure ``apply_dynamic_vars`` function and the worker-seam integration
(``_register_dynamic_vars_receiver``) — no real LiveKit room spin-up.
The wire-contract constant is frozen here so a rename on either side causes a test failure.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from usan_agent.dynamic_vars import DYNAMIC_VARS_TOPIC, apply_dynamic_vars
from usan_agent.worker import _register_dynamic_vars_receiver


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


# ---------------------------------------------------------------------------
# Integration tests for _register_dynamic_vars_receiver (worker seam)
# ---------------------------------------------------------------------------


def _make_fake_ctx() -> tuple[MagicMock, list]:
    """Return a fake JobContext and the list of registered data_received callbacks."""
    callbacks: list = []

    def _on(event: str, cb: object) -> None:
        if event == "data_received":
            callbacks.append(cb)

    room = MagicMock()
    room.name = "test-room"
    room.on = _on
    ctx = MagicMock()
    ctx.room = room
    return ctx, callbacks


def _make_packet(topic: str, data: bytes) -> MagicMock:
    pkt = MagicMock()
    pkt.topic = topic
    pkt.data = data
    return pkt


@pytest.mark.asyncio
async def test_receiver_awaits_update_instructions_with_full_template() -> None:
    """_register_dynamic_vars_receiver must schedule update_instructions as a coroutine
    and pass the fully-substituted COMPLETE instruction text (template + static suffix)."""
    ctx, callbacks = _make_fake_ctx()
    agent = MagicMock()
    agent.update_instructions = AsyncMock()

    template = "Hello {{first_name}}, time for your check-in.\n\nSMS suffix here."
    current_vars: dict[str, str] = {"first_name": "there"}

    _register_dynamic_vars_receiver(ctx, agent, current_vars, template)
    assert len(callbacks) == 1, "expected exactly one data_received callback registered"

    # Fire a packet on the correct topic.
    pkt = _make_packet(DYNAMIC_VARS_TOPIC, json.dumps({"first_name": "Ada"}).encode())
    callbacks[0](pkt)

    # The coroutine is scheduled via ensure_future; let the event loop run it.
    await asyncio.sleep(0)

    agent.update_instructions.assert_awaited_once_with(
        "Hello Ada, time for your check-in.\n\nSMS suffix here."
    )


@pytest.mark.asyncio
async def test_receiver_ignores_wrong_topic() -> None:
    """Packets on a different topic must not trigger update_instructions."""
    ctx, callbacks = _make_fake_ctx()
    agent = MagicMock()
    agent.update_instructions = AsyncMock()

    _register_dynamic_vars_receiver(ctx, agent, {"first_name": "there"}, "Hello {{first_name}}")

    pkt = _make_packet("some/other/topic", json.dumps({"first_name": "Ada"}).encode())
    callbacks[0](pkt)
    await asyncio.sleep(0)

    agent.update_instructions.assert_not_called()


@pytest.mark.asyncio
async def test_receiver_skips_update_when_flow_active() -> None:
    """#2 fix: on a flow-active RagAgent, the flow owns the prompt — a mid-call var
    update must NOT clobber it via update_instructions."""
    from usan_agent.rag_agent import RagAgent
    from usan_agent.settings import Settings

    settings = Settings(
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="s" * 32,
        LIVEKIT_URL="wss://example.com",
        CARTESIA_API_KEY="c",
        GCP_PROJECT="proj",
        DEFAULT_CARTESIA_VOICE_ID="v",
        API_BASE_URL="https://api.example.com",
        JWT_SIGNING_KEY="j" * 32,
        FLOW_RUNTIME_VOICE_ENABLED=True,
    )
    agent = RagAgent(instructions="base", call_id="c-1", settings=settings)
    agent.update_instructions = AsyncMock()  # type: ignore[method-assign]
    # Simulate a call that has bound to a flow (flow_active reads these three fields).
    agent._flow_ever_bound = True
    assert agent.flow_active is True

    ctx, callbacks = _make_fake_ctx()
    _register_dynamic_vars_receiver(ctx, agent, {"first_name": "there"}, "Hello {{first_name}}")

    pkt = _make_packet(DYNAMIC_VARS_TOPIC, json.dumps({"first_name": "Ada"}).encode())
    callbacks[0](pkt)
    await asyncio.sleep(0)

    agent.update_instructions.assert_not_called()


@pytest.mark.asyncio
async def test_receiver_applies_update_when_flow_not_active() -> None:
    """A RagAgent that never bound a flow (flow_active False) still gets mid-call var
    updates applied normally — the skip is specific to flow-active calls."""
    from usan_agent.rag_agent import RagAgent
    from usan_agent.settings import Settings

    settings = Settings(
        LIVEKIT_API_KEY="k",
        LIVEKIT_API_SECRET="s" * 32,
        LIVEKIT_URL="wss://example.com",
        CARTESIA_API_KEY="c",
        GCP_PROJECT="proj",
        DEFAULT_CARTESIA_VOICE_ID="v",
        API_BASE_URL="https://api.example.com",
        JWT_SIGNING_KEY="j" * 32,
        FLOW_RUNTIME_VOICE_ENABLED=False,
    )
    agent = RagAgent(instructions="base", call_id="c-1", settings=settings)
    agent.update_instructions = AsyncMock()  # type: ignore[method-assign]
    assert agent.flow_active is False

    ctx, callbacks = _make_fake_ctx()
    _register_dynamic_vars_receiver(ctx, agent, {"first_name": "there"}, "Hello {{first_name}}")

    pkt = _make_packet(DYNAMIC_VARS_TOPIC, json.dumps({"first_name": "Ada"}).encode())
    callbacks[0](pkt)
    await asyncio.sleep(0)

    agent.update_instructions.assert_awaited_once_with("Hello Ada")
