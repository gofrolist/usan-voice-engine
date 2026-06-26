"""CreateWebCallRequest: required agent_id, accepts the heavier optional fields."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from usan_api.compat.schemas.calls import CreateWebCallRequest


def test_minimal_request_requires_only_agent_id() -> None:
    req = CreateWebCallRequest(agent_id="agent_abc")
    assert req.agent_id == "agent_abc"
    assert req.agent_override is None


def test_accepts_heavier_optional_fields() -> None:
    req = CreateWebCallRequest(
        agent_id="agent_abc",
        agent_version="latest",
        agent_override={"voice_id": "v"},
        metadata={"external_id": "e1"},
        retell_llm_dynamic_variables={"name": "Pat"},
        current_node_id="n1",
        current_state="s1",
    )
    assert req.agent_override == {"voice_id": "v"}
    assert req.current_node_id == "n1"


def test_missing_agent_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateWebCallRequest()


def test_empty_agent_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateWebCallRequest(agent_id="")


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        CreateWebCallRequest(agent_id="agent_abc", bogus="x")
