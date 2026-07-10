"""Surface 3: build LiveKit raw-schema tools for client-defined HTTP tools (design 2026-07-09).

Each projected ``ExternalToolSpec`` (name / description / parameters — the worker never sees a
tool's url or the caller secret) becomes a ``RawFunctionTool`` the LLM can call. Execution is
delegated to ``apps/api`` (``POST /v1/tools/external``): the handler forwards only the
call-scoped name + arguments and relays the tool's result to the model. Like every builtin tool
(``check_in._do_*``) the handler NEVER raises into the session — any failure returns a calm
spoken fallback so a transient client-edge error can't crash a live call.
"""

import json
from typing import Any

from livekit.agents import RunContext, function_tool
from loguru import logger

from usan_agent import api_client
from usan_agent.agent_config import ExternalToolSpec
from usan_agent.settings import Settings

# Spoken when the tool call fails (network, non-2xx, timeout). Mirrors the builtins' benign
# confirmations: the contact hears something calm and the conversation continues.
_EXTERNAL_TOOL_FALLBACK = "I had trouble doing that just now, but let's keep going."


def _raw_schema(spec: ExternalToolSpec) -> dict[str, Any]:
    # The LLM sees this verbatim (LiveKit raw_schema): the client's JSON-Schema unchanged.
    return {"name": spec.name, "description": spec.description, "parameters": spec.parameters}


async def _terminate_call(ctx: RunContext, *, tool: str) -> None:
    """Hang up after a client tool with ``terminates_call=True`` (Retell
    end_call_after_speech_with_success). Mirrors the builtin end_call: say goodbye, delete the
    room, shut down. Best-effort — never raises into the session. A no-op when the session has no
    ``CheckInData`` userdata (e.g. greet-only), since there is no call state to tear down.
    """
    data = getattr(ctx, "userdata", None)
    if data is None or getattr(data, "job_ctx", None) is None:
        return
    try:
        # Local import breaks the check_in ↔ external_tools module cycle (check_in imports
        # build_external_tools at load). _hang_up is the canonical teardown the builtins use.
        from usan_agent.check_in import _hang_up

        await _hang_up(data, ctx.session)
    except Exception:
        logger.bind(tool=tool).warning("terminates_call hang-up failed")


def _make_external_tool(spec: ExternalToolSpec, *, call_id: str, settings: Settings) -> Any:
    async def _handler(ctx: RunContext, raw_arguments: dict[str, object]) -> str:
        try:
            result = await api_client.call_external_tool(
                call_id, settings, name=spec.name, arguments=dict(raw_arguments)
            )
        except Exception:
            # PHI-safe: name only, never the args/response. Never re-raise into the session. On
            # failure we do NOT hang up — Retell ends only after a *successful* call, and a
            # transient client-edge error must leave the conversation running.
            logger.bind(call_id=call_id, tool=spec.name).warning("external tool call failed")
            return _EXTERNAL_TOOL_FALLBACK
        if spec.terminates_call:
            await _terminate_call(ctx, tool=spec.name)
        try:
            return json.dumps(result)
        except (TypeError, ValueError):
            return str(result)

    return function_tool(_handler, raw_schema=_raw_schema(spec))


def build_external_tools(
    specs: list[ExternalToolSpec], *, call_id: str | None, settings: Settings | None
) -> list[Any]:
    """Build raw-schema tools for a call's external tools. Requires ``call_id`` + ``settings``
    (the handler delegates to the JWT-scoped API proxy); without a live-call context there is
    nothing to execute against, so none are built."""
    if not specs or call_id is None or settings is None:
        return []
    return [_make_external_tool(s, call_id=call_id, settings=settings) for s in specs]


async def _noop_handler(raw_arguments: dict[str, object]) -> str:
    # Sandbox (session_kind=="test"): return a canned string, make NO API call.
    return "Done (test mode)."


def build_external_test_tools(specs: list[ExternalToolSpec]) -> list[Any]:
    """Sandbox parallel of ``build_external_tools``: raw-schema tools that return a canned
    string and touch no network, so a pre-publish Test Audio run exercises the same tool
    surface without egress (mirrors ``check_in._TEST_TOOL_REGISTRY``)."""
    return [function_tool(_noop_handler, raw_schema=_raw_schema(s)) for s in specs]
