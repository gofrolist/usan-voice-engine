"""Surface 3: translate a RetellAI ``general_tools`` list into native tool config.

RetellAI ``custom`` tools (and the client's dashboard-export flat shape, which omits
``type``) become executable ``ExternalToolSpec`` dicts written into
``config["tools"]["external_tools"]``. The built-in ``end_call`` type maps onto our
lifecycle builtin (unioned into ``enabled``). The ``kb_lookup`` built-in — a placeholder
URL with no HTTP backing — is NOT turned into a tool: KB retrieval is handled natively via
the agent's ``knowledge_base_ids`` binding, so it is recorded and skipped. Other built-in
types (transfer_call, press_digit, …) are out of scope and skipped, never fabricated.

Called ONLY when ``COMPAT_EXTERNAL_TOOLS_ENABLED`` (agent_bridge). The raw ``general_tools``
list is still echoed verbatim via ``compat_extras`` regardless, so the RetellAI object
round-trips with no field loss (parity §0).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The client's ``kb_lookup`` tool ships a literal placeholder in its url field
# ("RETELL_BUILT_IN — if your KB is uploaded to Retell…"). Detect it so we never
# create an ExternalToolSpec that would POST a non-URL at call time.
_KB_PLACEHOLDER_PREFIX = "RETELL_BUILT_IN"

# Retell custom-tool timeout is milliseconds; ExternalToolSpec.timeout_s is seconds
# clamped to [1, 30] (matches the schema range so translation never produces an
# out-of-range spec that would then 422 in _validate_config for a benign reason).
_DEFAULT_TIMEOUT_S = 10.0
_MIN_TIMEOUT_S = 1.0
_MAX_TIMEOUT_S = 30.0


@dataclass
class TranslatedTools:
    """Result of translating a ``general_tools`` list.

    ``external_tools`` are ExternalToolSpec-shaped dicts (validated downstream by
    ``AgentConfig.model_validate`` in the bridge). ``enable`` are builtin tool names to
    union into ``config["tools"]["enabled"]``. ``kb_lookup_present`` flags that the agent
    asked for KB retrieval (handled natively, not as an HTTP tool). ``skipped`` records
    entries we did not translate (unknown built-in types / non-executable junk) for logging.
    """

    external_tools: list[dict[str, Any]] = field(default_factory=list)
    enable: list[str] = field(default_factory=list)
    kb_lookup_present: bool = False
    skipped: list[str] = field(default_factory=list)


def _is_kb_placeholder(entry: dict[str, Any]) -> bool:
    url = entry.get("url")
    if isinstance(url, str) and url.strip().startswith(_KB_PLACEHOLDER_PREFIX):
        return True
    # Defensive: a KB built-in may also self-identify by name/type without the placeholder.
    return entry.get("name") == "kb_lookup" or entry.get("type") == "knowledge_base"


def _timeout_s(entry: dict[str, Any]) -> float:
    raw_ms = entry.get("timeout_ms")
    if not isinstance(raw_ms, (int, float)) or raw_ms <= 0:
        return _DEFAULT_TIMEOUT_S
    return max(_MIN_TIMEOUT_S, min(_MAX_TIMEOUT_S, raw_ms / 1000.0))


def _to_spec(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Build an ExternalToolSpec dict from a custom/flat tool entry, or None if it is not
    executable (missing name or url). A missing description falls back to the name; missing
    parameters default to a no-arg object schema. Structural validity is enforced later by
    ``AgentConfig.model_validate`` — this only shapes the dict."""
    name = entry.get("name")
    url = entry.get("url")
    if not isinstance(name, str) or not name or not isinstance(url, str) or not url:
        return None
    method = str(entry.get("method") or "POST").upper()
    if method not in ("POST", "GET"):
        method = "POST"
    parameters = entry.get("parameters")
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    return {
        "name": name,
        "description": entry.get("description") or name,
        "url": url,
        "method": method,
        "parameters": parameters,
        "timeout_s": _timeout_s(entry),
        "speak_during_execution": bool(entry.get("speak_during_execution", False)),
    }


def translate_general_tools(raw: list[Any] | None) -> TranslatedTools:
    """Classify each ``general_tools`` entry (see module docstring). Tolerant: a non-dict
    entry is skipped, never raised on."""
    result = TranslatedTools()
    if not raw:
        return result
    for entry in raw:
        if not isinstance(entry, dict):
            result.skipped.append(repr(entry)[:40])
            continue
        ttype = entry.get("type")
        # KB built-in first — it carries a url (the placeholder) so it must be caught
        # before the custom/flat branch would treat it as an HTTP tool.
        if _is_kb_placeholder(entry):
            result.kb_lookup_present = True
            continue
        if ttype == "end_call":
            if "end_call" not in result.enable:
                result.enable.append("end_call")
            continue
        # 'custom' (Retell API) or the client's flat dashboard-export shape (no type).
        if ttype == "custom" or ttype is None:
            spec = _to_spec(entry)
            if spec is not None:
                result.external_tools.append(spec)
            else:
                result.skipped.append(str(entry.get("name") or entry.get("type") or "?"))
            continue
        # Any other built-in type (transfer_call, press_digit, …): out of scope.
        result.skipped.append(str(ttype))
    return result
