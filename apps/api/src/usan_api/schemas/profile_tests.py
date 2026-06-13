"""Request/response schemas for the pre-publish agent test endpoints (US5, T049).

Two sandboxed test modes (design research R3 / contract admin-api.md):

- ``POST /v1/admin/profiles/{id}/test/llm`` — a text simulation run against Vertex
  AI with stub (no-op) tools. Body carries the chat ``messages``, synthetic
  ``sample_vars``, and an optional draft ``config`` (omitted → the stored draft).
- ``POST /v1/admin/profiles/{id}/test/audio`` — mints a join-only browser LiveKit
  token and dispatches the agent in ``session_kind="test"``; returns the room +
  short-TTL token + url for ``Room.connect``.

``sample_vars`` are admin-supplied SYNTHETIC values only — no real contact PHI is
loaded in either mode (Constitution II). ``config`` reuses the frozen
``AgentConfig`` so the test runs exactly the document the editor would publish.
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints

from usan_api.schemas.agent_config import AgentConfig

# A text-test run is capped at this many chat turns and this much sample text so a
# single test cannot rack up unbounded Vertex spend (research R3 key risks).
_MAX_MESSAGES = 50
_MAX_CONTENT_LEN = 8000
_MAX_SAMPLE_VARS = 100
_MAX_SAMPLE_VALUE_LEN = 2000
# Cap each sample-var NAME length too (values are capped in bounded_sample_vars). Real
# variable names are <=64 chars; this just bounds a pathological key (defense in depth).
_MAX_SAMPLE_KEY_LEN = 128

# A length-bounded sample-var key so a single test cannot carry unbounded key strings.
SampleVarKey = Annotated[str, StringConstraints(max_length=_MAX_SAMPLE_KEY_LEN)]


class TestMessage(BaseModel):
    """One chat turn in the text-test transcript."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=_MAX_CONTENT_LEN)


class TestLlmRequest(BaseModel):
    messages: list[TestMessage] = Field(min_length=1, max_length=_MAX_MESSAGES)
    # name -> synthetic sample value. Capped count + length so a test run is bounded.
    sample_vars: dict[SampleVarKey, str] = Field(default_factory=dict, max_length=_MAX_SAMPLE_VARS)
    # Omitted → use the profile's stored draft_config (re-validated handler-side).
    config: AgentConfig | None = None

    def bounded_sample_vars(self) -> dict[str, str]:
        """``sample_vars`` with each value length-capped (defense against a huge value)."""
        return {k: v[:_MAX_SAMPLE_VALUE_LEN] for k, v in self.sample_vars.items()}


class TestToolCall(BaseModel):
    """A tool the model asked to call during the simulation (echoed to the UI)."""

    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class TestLlmResponse(BaseModel):
    assistant: str
    tool_calls: list[TestToolCall] = Field(default_factory=list)


class TestAudioRequest(BaseModel):
    sample_vars: dict[SampleVarKey, str] = Field(default_factory=dict, max_length=_MAX_SAMPLE_VARS)
    config: AgentConfig | None = None

    def bounded_sample_vars(self) -> dict[str, str]:
        return {k: v[:_MAX_SAMPLE_VALUE_LEN] for k, v in self.sample_vars.items()}


class TestAudioResponse(BaseModel):
    url: str  # externally reachable wss:// LiveKit URL for Room.connect
    token: str  # short-TTL join-only browser access token
    room: str  # the throwaway room name (usan-test-<uuid>)
