"""Vertex AI text-test client for the pre-publish agent simulation (US5 / FR-025).

The text test calls Vertex AI DIRECTLY via the ``google-genai`` SDK with
``vertexai=True`` + ADC (the attached service account) — NEVER the Gemini Developer
API (Constitution II PHI containment). It is decoupled from the agent's LiveKit
``google.LLM`` plugin: ``apps/api`` runs its own one-shot turn so the editor can
simulate the prompt without a phone call.

``run_vertex_turn`` performs a SINGLE generation turn; the router
(``admin_profile_tests``) owns the bounded model→stub-tool→continue loop. The SDK
call runs in a worker thread (``asyncio.to_thread``) because ``google-genai``'s
sync client is blocking and the API request handler is async.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from usan_api.settings import Settings


@dataclass(frozen=True)
class VertexToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VertexTurn:
    """One model turn: free text and/or the tool calls the model requested."""

    text: str = ""
    tool_calls: list[VertexToolCall] = field(default_factory=list)


def _build_config(
    *, temperature: float | None, system_instruction: str, tools: list[dict[str, Any]]
) -> Any:
    from google.genai import types

    declarations = [
        types.FunctionDeclaration(
            name=str(t["name"]),
            description=str(t.get("description", "")),
            parameters_json_schema=t.get("parameters_json_schema")
            or {"type": "object", "properties": {}},
        )
        for t in tools
    ]
    kwargs: dict[str, Any] = {"system_instruction": system_instruction}
    if declarations:
        kwargs["tools"] = [types.Tool(function_declarations=declarations)]
    if temperature is not None:
        kwargs["temperature"] = temperature
    return types.GenerateContentConfig(**kwargs)


def _parse_response(response: Any) -> VertexTurn:
    """Extract free text + function calls from a genai response, defensively."""
    text = getattr(response, "text", None) or ""
    calls: list[VertexToolCall] = []
    for fc in getattr(response, "function_calls", None) or []:
        name = getattr(fc, "name", None)
        if not name:
            continue
        args = getattr(fc, "args", None) or {}
        calls.append(VertexToolCall(name=str(name), args=dict(args)))
    return VertexTurn(text=text, tool_calls=calls)


def _generate_sync(
    *,
    model: str,
    config: Any,
    contents: list[dict[str, Any]],
    settings: Settings,
) -> Any:
    from google import genai

    # vertexai=True + project/location → ADC (the attached VM service account),
    # NOT the Gemini Developer API. No API key is ever passed (Constitution II).
    client = genai.Client(
        vertexai=True,
        project=settings.gcp_project,
        location=settings.vertex_location,
    )
    try:
        return client.models.generate_content(model=model, contents=contents, config=config)
    finally:
        client.close()


async def run_vertex_turn(
    *,
    model: str,
    temperature: float | None,
    system_instruction: str,
    tools: list[dict[str, Any]],
    contents: list[dict[str, Any]],
    settings: Settings,
) -> VertexTurn:
    """Run one Vertex generation turn off the event loop and parse the result."""
    config = _build_config(
        temperature=temperature, system_instruction=system_instruction, tools=tools
    )
    response = await asyncio.to_thread(
        _generate_sync, model=model, config=config, contents=contents, settings=settings
    )
    return _parse_response(response)
