from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import yaml

ORACLE_DIR = Path(__file__).parent / "oracle"
ORACLE_PATH = ORACLE_DIR / "openapi-final.yaml"
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


@cache
def load_oracle() -> dict[str, Any]:
    # CSafeLoader: the oracle is 530KB and the pure-Python loader costs ~0.26s
    # per xdist worker (~0.03s with libyaml) — first-touch cost billed to
    # whichever conformance test runs first on each worker.
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    return yaml.load(ORACLE_PATH.read_text(), Loader=loader)


@cache
def oracle_operations() -> frozenset[tuple[str, str]]:
    """(METHOD, path) for every operation in the spec."""
    paths = load_oracle()["paths"]
    return frozenset(
        (method.upper(), path)
        for path, item in paths.items()
        for method in item
        if method.lower() in _HTTP_METHODS
    )
