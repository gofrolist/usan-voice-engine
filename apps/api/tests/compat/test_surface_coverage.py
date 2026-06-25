"""T047 — compat surface-coverage test (feature 003, US-cross-cutting).

Every operation in the oracle OpenAPI must be either:
  1. Served by the compat app (real route),
  2. 501-stubbed in unsupported.py (documented not-supported), or
  3. Explicitly listed in KNOWN_GAPS (deferred to sub-PR 1b).

KNOWN_GAPS are 501-router path-drift fixes and the 6 Pri-1 new endpoints that
sub-PR 1b will resolve.  When 1b lands, KNOWN_GAPS shrinks to empty.
"""

from __future__ import annotations

import re

from tests.compat.oracle_loader import oracle_operations
from usan_api.compat.app import build_compat_app
from usan_api.settings import get_settings

_PARAM = re.compile(r"\{[^}]+\}")


def _norm(path: str) -> str:
    """Collapse all path-param placeholders to ``{}`` so naming differences
    (e.g. ``{call_id}`` vs ``{resource_id}``) never cause false "missing"."""
    return _PARAM.sub("{}", path)


# ---------------------------------------------------------------------------
# KNOWN_GAPS: every entry is deferred to sub-PR 1b.
# Format: (METHOD, normalized oracle path)
# Each entry carries a ``# 1b: <reason>`` comment explaining the category:
#   - "new-endpoint": one of the 6 Pri-1 endpoints not yet implemented
#   - "501-drift":    our unsupported.py stub is at a different path than the
#                     oracle's (path renamed or param dropped) — fix in 1b
# ---------------------------------------------------------------------------
KNOWN_GAPS: frozenset[tuple[str, str]] = frozenset(
    {
        # --- 5 Pri-1 new endpoints (not yet implemented) ---
        ("PATCH", "/v2/update-live-call/{}"),  # 1b: new-endpoint
        ("DELETE", "/delete-agent-version/{}"),  # 1b: new-endpoint
        ("POST", "/v2/list-agents"),  # 1b: new-endpoint
        ("POST", "/publish-agent/{}"),  # 1b: new-endpoint
        # --- 501-router path-drift (stub exists but path is wrong) ---
        # Oracle: /v3/list-chats  →  our stub: /list-chat (missing /v3 prefix + plural)
        ("POST", "/v3/list-chats"),  # 1b: 501-drift (stub at /list-chat)
        # Oracle: /add-community-voice  →  our stub: /add-voice (wrong name)
        ("POST", "/add-community-voice"),  # 1b: 501-drift (stub at /add-voice)
        # Oracle: /search-community-voice  →  our stub: /search-voice (wrong name)
        ("POST", "/search-community-voice"),  # 1b: 501-drift (stub at /search-voice)
        # Oracle: /create-test-case-definition  →  our stub: /create-test-case (truncated)
        ("POST", "/create-test-case-definition"),  # 1b: 501-drift (stub at /create-test-case)
        # Oracle: /v2/list-conversation-flows  →  our stub: /list-conversation-flows (missing /v2)
        ("GET", "/v2/list-conversation-flows"),  # 1b: 501-drift (stub missing /v2 prefix)
        # Oracle: /v2/list-phone-numbers  →  our stub: /list-phone-numbers (missing /v2)
        ("GET", "/v2/list-phone-numbers"),  # 1b: 501-drift (stub missing /v2 prefix)
        # Oracle: /get-mcp-tools/{agent_id}  →  our stub: /get-mcp-tools (drops the param)
        ("GET", "/get-mcp-tools/{}"),  # 1b: 501-drift (stub at /get-mcp-tools, drops param)
        # Oracle: /v2/list-export-requests  →  our stub: /list-export-requests (missing /v2)
        ("GET", "/v2/list-export-requests"),  # 1b: 501-drift (stub missing /v2 prefix)
        # Oracle: /agent-playground-completion/{agent_id}  →  stub: /agent-playground-completion
        ("POST", "/agent-playground-completion/{}"),  # 1b: 501-drift (stub drops param)
        # New oracle ops not covered by any stub yet
        # Oracle: /create-conversation-flow-component  →  no stub
        ("POST", "/create-conversation-flow-component"),  # 1b: 501-drift (no stub)
        # Oracle: /v2/list-conversation-flow-components  →  no stub
        ("GET", "/v2/list-conversation-flow-components"),  # 1b: 501-drift (no stub)
        # Oracle: /get-conversation-flow-component/{id}  →  no stub
        ("GET", "/get-conversation-flow-component/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /update-conversation-flow-component/{id}  →  no stub
        ("PATCH", "/update-conversation-flow-component/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /delete-conversation-flow-component/{id}  →  no stub
        ("DELETE", "/delete-conversation-flow-component/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /delete-test-case-definition/{id}  →  no stub
        ("DELETE", "/delete-test-case-definition/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /get-test-case-definition/{id}  →  no stub
        ("GET", "/get-test-case-definition/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /v2/list-test-case-definitions  →  no stub
        ("GET", "/v2/list-test-case-definitions"),  # 1b: 501-drift (no stub)
        # Oracle: /update-test-case-definition/{id}  →  no stub
        ("PUT", "/update-test-case-definition/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /get-test-run/{id}  →  no stub
        ("GET", "/get-test-run/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /v2/list-test-runs/{id}  →  no stub
        ("GET", "/v2/list-test-runs/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /get-batch-test/{id}  →  no stub
        ("GET", "/get-batch-test/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /v2/list-batch-tests  →  no stub
        ("GET", "/v2/list-batch-tests"),  # 1b: 501-drift (no stub)
        # Oracle: /delete-knowledge-base-source/{kb_id}/source/{src_id}  →  no stub
        ("DELETE", "/delete-knowledge-base-source/{}/source/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /add-knowledge-base-sources/{kb_id}  →  no stub
        ("POST", "/add-knowledge-base-sources/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /create-sms-chat  →  no stub
        ("POST", "/create-sms-chat"),  # 1b: 501-drift (no stub)
        # Oracle: /delete-chat-agent/{id}  →  no stub
        ("DELETE", "/delete-chat-agent/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /get-chat-agent/{id}  →  no stub
        ("GET", "/get-chat-agent/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /get-chat-agent-versions/{id}  →  no stub
        ("GET", "/get-chat-agent-versions/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /list-chat-agents  →  no stub
        ("GET", "/list-chat-agents"),  # 1b: 501-drift (no stub)
        # Oracle: /update-chat-agent/{id}  →  no stub
        ("PATCH", "/update-chat-agent/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /publish-chat-agent/{id}  →  no stub
        ("POST", "/publish-chat-agent/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /end-chat/{id}  →  no stub
        ("PATCH", "/end-chat/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /update-chat/{id}  →  no stub
        ("PATCH", "/update-chat/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /rerun-call-analysis/{id}  →  no stub
        ("PUT", "/rerun-call-analysis/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /rerun-chat-analysis/{id}  →  no stub
        ("PUT", "/rerun-chat-analysis/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /v2/list-retell-llms  →  no stub
        ("GET", "/v2/list-retell-llms"),  # 1b: 501-drift (no stub)
        # Oracle: /create-agent-version/{id}  →  no stub
        ("POST", "/create-agent-version/{}"),  # 1b: 501-drift (no stub)
        # Oracle: /delete-chat/{id}  →  no stub (chat stubs only cover create/get/list)
        ("DELETE", "/delete-chat/{}"),  # 1b: 501-drift (no stub for delete-chat)
    }
)


def _served() -> set[tuple[str, str]]:
    """Collect all (METHOD, normalized-path) pairs from the compat app's route table."""
    app = build_compat_app(get_settings())
    out: set[tuple[str, str]] = set()
    for route in app.routes:
        for method in getattr(route, "methods", set()) or set():
            out.add((method, _norm(route.path)))
    return out


def test_every_oracle_op_is_served_or_501_or_known_gap() -> None:
    """Assert that every oracle operation is either served, 501-stubbed, or in KNOWN_GAPS.

    Any op missing from all three buckets is a genuine coverage hole — a real in-scope
    endpoint we should serve but don't, and must NOT be silently added to KNOWN_GAPS.
    """
    served = _served()
    oracle = {(m, _norm(p)) for (m, p) in oracle_operations()}
    missing = sorted(op for op in oracle if op not in served and op not in KNOWN_GAPS)
    assert not missing, (
        f"oracle ops neither served, 501-stubbed, nor known-gap: {missing!r}\n"
        "If these are genuinely deferred to 1b, add them to KNOWN_GAPS with a "
        "# 1b: <reason> comment. If they are in-scope endpoints we should serve, "
        "DO NOT add to KNOWN_GAPS — fix the implementation."
    )
