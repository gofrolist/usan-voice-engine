"""Out-of-scope RetellAI endpoints → documented ``501 not_supported`` (feature 003,
US5, FR-053 / SC-009).

A CRM that hits an endpoint the USAN voice engine does not implement gets a clean,
RetellAI-shaped ``{status:501, message:"not_supported: <endpoint>"}`` instead of a
bare 404 — and the endpoints are listed in the compat OpenAPI so the gap is explicit.
The set is the contracts/endpoints.md "Out-of-scope" list (conversation-flow,
knowledge-base, chat, web-call, voice add/clone/search, test-suite, phone-number,
MCP tools, export requests, agent playground). Parametrized paths use a uniform
``{resource_id}`` placeholder — the id is never read; the stub 501s regardless.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NoReturn

from fastapi import APIRouter

from usan_api.compat.errors import CompatError

router = APIRouter(tags=["compat-unsupported"])

_UNSUPPORTED: tuple[tuple[str, str], ...] = (
    # Conversation flow
    ("POST", "/create-conversation-flow"),
    ("GET", "/get-conversation-flow/{resource_id}"),
    ("PATCH", "/update-conversation-flow/{resource_id}"),
    ("DELETE", "/delete-conversation-flow/{resource_id}"),
    ("GET", "/list-conversation-flows"),
    # Knowledge base
    ("POST", "/create-knowledge-base"),
    ("GET", "/get-knowledge-base/{resource_id}"),
    ("DELETE", "/delete-knowledge-base/{resource_id}"),
    ("GET", "/list-knowledge-bases"),
    # Chat
    ("POST", "/create-chat"),
    ("POST", "/create-chat-completion"),
    ("GET", "/get-chat/{resource_id}"),
    ("GET", "/list-chat"),
    ("POST", "/create-chat-agent"),
    # Web call
    ("POST", "/v2/create-web-call"),
    # Voice authoring (add / clone / search) — distinct from the read-only catalog
    ("POST", "/add-voice"),
    ("POST", "/clone-voice"),
    ("POST", "/search-voice"),
    # Test suite
    ("POST", "/create-batch-test"),
    ("POST", "/create-test-case"),
    ("POST", "/create-test-run"),
    # Phone-number management (the engine owns its own Telnyx/LiveKit numbers)
    ("POST", "/create-phone-number"),
    ("POST", "/import-phone-number"),
    ("GET", "/list-phone-numbers"),
    ("GET", "/get-phone-number/{resource_id}"),
    ("PATCH", "/update-phone-number/{resource_id}"),
    ("DELETE", "/delete-phone-number/{resource_id}"),
    # MCP / export / playground
    ("GET", "/get-mcp-tools"),
    ("GET", "/list-export-requests"),
    ("GET", "/get-export-request/{resource_id}"),
    ("POST", "/agent-playground-completion"),
)


def _make_stub(endpoint: str) -> Callable[..., Awaitable[NoReturn]]:
    """Build a 501 handler. The uniform ``resource_id`` is a path param where the
    route declares one and an ignored optional query param otherwise; the stub 501s
    regardless. FastAPI's default operation id incorporates the path, so the shared
    handler name stays unique across endpoints."""

    async def stub(resource_id: str | None = None) -> NoReturn:
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
