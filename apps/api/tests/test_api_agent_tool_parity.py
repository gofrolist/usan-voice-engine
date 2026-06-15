"""T080 (Polish): api↔agent TOOL parity contract — the two mirrors cannot drift.

`apps/api` and `services/agent` must not import each other (Service Isolation,
Constitution I). The agent hand-mirrors the API's closed tool set in three places:

  * ``check_in._TOOL_REGISTRY``       — live ``@function_tool`` callables
  * ``check_in._TEST_TOOL_REGISTRY``  — no-op stubs for pre-publish Test Audio
  * ``agent_config.DEFAULT_AGENT_CONFIG.tools.enabled`` — the shipped default

If a tool is added to the API catalog but not to all three agent mirrors (or vice
versa), a live call would silently drop or mis-offer that tool. This contract pins
all of them to the single API source of truth (``schemas.agent_config.TOOL_NAMES``)
and to the canonical offered order.

``check_in.py`` imports ``livekit.agents`` (absent from the apps/api venv), so the
registries are read by **AST parse of the source**, never by import — keeping this a
true cross-unit comparison with no new import edge. The builtin-variable and
prompt-body halves of the api↔agent contract live in ``test_prompt_substitution_parity``.
"""

import ast
import importlib.util
from pathlib import Path

from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG as API_DEFAULT
from usan_api.schemas.agent_config import TOOL_NAMES

_AGENT_SRC = Path(__file__).resolve().parents[3] / "services" / "agent" / "src" / "usan_agent"
_CHECK_IN = _AGENT_SRC / "check_in.py"
_AGENT_CONFIG = _AGENT_SRC / "agent_config.py"

# Canonical offered order = the API default-enabled list. Pin it once to the closed set.
_API_ENABLED: list[str] = list(API_DEFAULT.tools.enabled)
assert set(_API_ENABLED) == set(TOOL_NAMES), "API default-enabled must equal the closed tool set"

_CHECK_IN_SRC = _CHECK_IN.read_text()


def _registry_keys(source: str, var_name: str) -> list[str]:
    """Extract the string keys of the module-level ``{...}`` dict assigned to ``var_name``.

    AST-only (no exec), so the livekit-bound module never has to import here.
    """
    tree = ast.parse(source)
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if not any(isinstance(t, ast.Name) and t.id == var_name for t in targets):
            continue
        value = node.value
        assert isinstance(value, ast.Dict), f"{var_name} is not a dict literal"
        keys: list[str] = []
        for key in value.keys:
            assert isinstance(key, ast.Constant), f"{var_name} has a non-constant key"
            assert isinstance(key.value, str), f"{var_name} has a non-string-literal key"
            keys.append(key.value)
        return keys
    raise AssertionError(f"{var_name} not found in check_in.py source")


def _registry_values(source: str, var_name: str) -> set[str]:
    """Names of the callables a module-level ``{...}`` registry maps to (the dict values).

    The two registries map a catalog name -> a function reference (``"log_wellness":
    log_wellness`` live, ``"log_wellness": noop_log_wellness`` in test mode), so the VALUES
    are what must line up with the decorated callables.
    """
    tree = ast.parse(source)
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if not any(isinstance(t, ast.Name) and t.id == var_name for t in targets):
            continue
        value = node.value
        assert isinstance(value, ast.Dict), f"{var_name} is not a dict literal"
        names: set[str] = set()
        for v in value.values:
            assert isinstance(v, ast.Name), f"{var_name} maps to a non-name value"
            names.add(v.id)
        return names
    raise AssertionError(f"{var_name} not found in check_in.py source")


def _function_tool_names(source: str) -> set[str]:
    """Names of every module-level ``@function_tool``-decorated async def.

    Handles both the bare ``@function_tool`` and the called ``@function_tool(...)`` forms.
    Includes BOTH the live tools and the ``noop_*`` test-mode stubs (both are decorated so
    Test Audio offers the identical surface). Each MUST be referenced by a registry or it
    is a dead tool the LLM can never invoke.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Name) and target.id == "function_tool":
                names.add(node.name)
    return names


def _load_agent_agent_config():
    """Load the agent's self-contained (pydantic-only) agent_config.py by file path."""
    spec = importlib.util.spec_from_file_location("usan_agent_agent_config_parity", _AGENT_CONFIG)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_live_tool_registry_keys_match_api_catalog():
    # Every API tool has a live agent-side callable, in the offered order, and the agent
    # registers no tool the API does not know about.
    assert _registry_keys(_CHECK_IN_SRC, "_TOOL_REGISTRY") == _API_ENABLED


def test_test_mode_registry_keys_match_api_catalog():
    # Pre-publish Test Audio offers the IDENTICAL tool set as a live call (no-op stubs),
    # so a test run exercises the same surface.
    assert _registry_keys(_CHECK_IN_SRC, "_TEST_TOOL_REGISTRY") == _API_ENABLED


def test_agent_default_enabled_matches_api_default_enabled():
    agent_cfg = _load_agent_agent_config()
    assert agent_cfg.DEFAULT_AGENT_CONFIG.tools.enabled == _API_ENABLED


def test_every_function_tool_is_registered():
    # No dead tools: every @function_tool callable (live AND noop) is referenced by one of
    # the two registries, and neither registry references a non-@function_tool. Closes the
    # "callable added but never wired into a registry" drift direction the catalog
    # comparison alone cannot see.
    decorated = _function_tool_names(_CHECK_IN_SRC)
    referenced = _registry_values(_CHECK_IN_SRC, "_TOOL_REGISTRY") | _registry_values(
        _CHECK_IN_SRC, "_TEST_TOOL_REGISTRY"
    )
    assert decorated == referenced
