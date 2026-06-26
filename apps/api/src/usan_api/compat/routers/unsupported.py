"""Out-of-scope RetellAI endpoints → documented ``501 not_supported`` (feature 003,
US5, FR-053 / SC-009).

A CRM that hits an endpoint the USAN voice engine does not implement gets a clean,
RetellAI-shaped ``{status:501, message:"not_supported: <endpoint>"}`` instead of a
bare 404 — and the endpoints are listed in the compat OpenAPI so the gap is explicit.
The set is the contracts/endpoints.md "Out-of-scope" list (conversation-flow,
conversation-flow-component, knowledge-base, chat, chat-agent, web-call, voice
add/clone/search, test-suite, phone-number, MCP tools, export requests, agent
playground, retell-llm, agent-version, analysis re-run).  Every path uses the
oracle's EXACT versioned path and param name (no uniform {resource_id} shorthand).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NoReturn

from fastapi import APIRouter, Request

from usan_api.compat.errors import CompatError

router = APIRouter(tags=["compat-unsupported"])

_UNSUPPORTED: tuple[tuple[str, str], ...] = (
    # --- Conversation flow ---
    ("POST", "/create-conversation-flow"),
    ("GET", "/get-conversation-flow/{conversation_flow_id}"),
    ("PATCH", "/update-conversation-flow/{conversation_flow_id}"),
    ("DELETE", "/delete-conversation-flow/{conversation_flow_id}"),
    ("GET", "/v2/list-conversation-flows"),
    # --- Conversation flow component ---
    ("POST", "/create-conversation-flow-component"),
    ("GET", "/v2/list-conversation-flow-components"),
    ("GET", "/get-conversation-flow-component/{conversation_flow_component_id}"),
    ("PATCH", "/update-conversation-flow-component/{conversation_flow_component_id}"),
    ("DELETE", "/delete-conversation-flow-component/{conversation_flow_component_id}"),
    # --- Knowledge base ---
    ("POST", "/create-knowledge-base"),
    ("GET", "/get-knowledge-base/{knowledge_base_id}"),
    ("DELETE", "/delete-knowledge-base/{knowledge_base_id}"),
    ("GET", "/list-knowledge-bases"),
    ("POST", "/add-knowledge-base-sources/{knowledge_base_id}"),
    ("DELETE", "/delete-knowledge-base-source/{knowledge_base_id}/source/{source_id}"),
    # --- Chat ---
    ("POST", "/create-chat"),
    ("POST", "/create-chat-completion"),
    ("GET", "/get-chat/{chat_id}"),
    ("POST", "/v3/list-chats"),
    ("DELETE", "/delete-chat/{chat_id}"),
    ("PATCH", "/end-chat/{chat_id}"),
    ("PATCH", "/update-chat/{chat_id}"),
    ("POST", "/create-sms-chat"),
    # --- Chat agent ---
    ("POST", "/create-chat-agent"),
    ("GET", "/list-chat-agents"),
    ("GET", "/get-chat-agent/{agent_id}"),
    ("GET", "/get-chat-agent-versions/{agent_id}"),
    ("PATCH", "/update-chat-agent/{agent_id}"),
    ("DELETE", "/delete-chat-agent/{agent_id}"),
    ("POST", "/publish-chat-agent/{agent_id}"),
    # --- Web call ---
    ("POST", "/v2/create-web-call"),
    # --- Voice authoring (add / clone / search) — distinct from read-only catalog ---
    ("POST", "/add-community-voice"),
    ("POST", "/clone-voice"),
    ("POST", "/search-community-voice"),
    # --- Test suite: batch-test ---
    ("POST", "/create-batch-test"),
    ("GET", "/get-batch-test/{test_case_batch_job_id}"),
    ("GET", "/v2/list-batch-tests"),
    # --- Test suite: test-case-definition ---
    ("POST", "/create-test-case-definition"),
    ("GET", "/get-test-case-definition/{test_case_definition_id}"),
    ("PUT", "/update-test-case-definition/{test_case_definition_id}"),
    ("DELETE", "/delete-test-case-definition/{test_case_definition_id}"),
    ("GET", "/v2/list-test-case-definitions"),
    # --- Test suite: test-run ---
    ("GET", "/get-test-run/{test_case_job_id}"),
    ("GET", "/v2/list-test-runs/{test_case_batch_job_id}"),
    # --- Phone-number management (the engine owns its own Telnyx/LiveKit numbers) ---
    ("POST", "/create-phone-number"),
    # --- MCP tools / export requests / agent playground ---
    ("GET", "/get-mcp-tools/{agent_id}"),
    ("POST", "/agent-playground-completion/{agent_id}"),
    # --- Retell LLM ---
    ("GET", "/v2/list-retell-llms"),
    # --- Agent versioning ---
    ("POST", "/create-agent-version/{agent_id}"),
    # --- Analysis re-run ---
    ("PUT", "/rerun-call-analysis/{call_id}"),
    ("PUT", "/rerun-chat-analysis/{chat_id}"),
)


def _make_stub(endpoint: str) -> Callable[..., Awaitable[NoReturn]]:
    """Build a 501 handler for an out-of-scope endpoint.

    Accepts a ``Request`` so FastAPI injects path parameters without needing
    them declared in the function signature — works for parameterless paths,
    single-param paths, and multi-segment paths like
    ``/delete-knowledge-base-source/{kb_id}/source/{src_id}``.
    FastAPI's default operation id incorporates the full path, keeping each
    auto-generated id unique across all stubs.
    """

    async def stub(_request: Request) -> NoReturn:
        raise CompatError(501, f"not_supported: {endpoint}")

    return stub


for _method, _path in _UNSUPPORTED:
    router.add_api_route(
        _path,
        _make_stub(_path),
        methods=[_method],
        status_code=501,
        response_model=None,
        summary="Not supported by the USAN voice engine (documented 501)",
    )
