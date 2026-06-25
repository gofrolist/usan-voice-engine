"""T047 — compat surface-coverage test (feature 003, US-cross-cutting).

Every operation in the oracle OpenAPI must be either:
  1. Served by the compat app (real route),
  2. 501-stubbed in unsupported.py (documented not-supported), or
  3. Explicitly listed in KNOWN_GAPS (empty since sub-PR 1b landed).
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


KNOWN_GAPS: frozenset[tuple[str, str]] = frozenset()


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


def test_501_stub_paths_match_oracle_exactly() -> None:
    """Every 501 stub path string (param names included) must be a real oracle path.

    Closes the _norm() blind spot where {resource_id} masks the oracle's {agent_id}, etc.
    """
    from usan_api.compat.routers.unsupported import _UNSUPPORTED

    oracle_paths = {(m, p) for (m, p) in oracle_operations()}
    for method, path in _UNSUPPORTED:
        assert (method, path) in oracle_paths, (
            f"501 stub {method} {path} is not an exact oracle path"
        )
