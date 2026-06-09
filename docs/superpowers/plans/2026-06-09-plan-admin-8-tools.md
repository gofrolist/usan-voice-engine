# Admin-UI Phase 3 — Data-driven Tool Catalog + Wellness Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded 4-tool list with a data-driven tool catalog and add three wellness tools — `flag_for_followup`, `schedule_callback`, and `send_sms` — end-to-end across `apps/api`, `services/agent`, and `apps/admin-ui`.

**Architecture:** Mirrors Phase 2's three-layer pattern (apps/api authoritative ↔ services/agent hand-mirror ↔ apps/admin-ui Zod/TanStack, no cross-imports). A global `TOOL_CATALOG` constant drives a closed-set validator (unknown tools hard-block) and a `GET /v1/admin/tool-catalog` endpoint; three new `/v1/tools/*` endpoints + tables (`follow_up_flags`, `callback_requests`, `sms_messages`) persist tool output; `send_sms` templates are PHI-hard-blocked at save and delivered post-call via a Telnyx Messaging outbox flushed at call completion. The four Parts execute **strictly A→B→C→D in one PR**: Part A owns every shared-symbol mutation (catalog, `TOOL_NAMES`, the final 7-tool default, the `_select_tools` rewrite, the registry-driven builder tests); B/C/D are purely additive.

**Tech Stack:** Python 3.14 (FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic, prometheus_client, httpx); Python 3.12 LiveKit Agents worker; React 18 + Vite + TS + Zod + react-hook-form + TanStack Query (Vitest); Telnyx Messaging API; Grafana dashboards-as-code.

**Source spec:** `docs/superpowers/specs/2026-06-09-admin-ui-phase3-tools-design.md`. This plan was drafted per-part against the real code and passed an adversarial integration review (zero violations / zero spec gaps).

**Two executor notes (already corrected inline below):** (1) `_select_tools` takes a `ToolsConfig` (not a bare list) — tests construct a config object. (2) admin routes rely on the router-level `require_admin_session`; the auth principal type is `AdminPrincipal` (there is no `AdminUser`).

---

## Part A — Tool catalog + ALL shared-surface ownership

### Task A1: API tool catalog (`tool_catalog.py` + catalog↔TOOL_NAMES test)

**Files:**
- Create: `apps/api/src/usan_api/schemas/tool_catalog.py`
- Test: `apps/api/tests/test_tool_catalog.py`

- [ ] Step 1: Write the failing test

```python
# apps/api/tests/test_tool_catalog.py
from usan_api.schemas.tool_catalog import (
    TOOL_CATALOG,
    TOOL_NAMES,
    ToolCatalogResponse,
    ToolSpec,
)


def test_catalog_has_exactly_seven_tools_in_order():
    names = [t.name for t in TOOL_CATALOG]
    assert names == [
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "flag_for_followup",
        "schedule_callback",
        "send_sms",
        "end_call",
    ]


def test_catalog_categories_and_flags():
    by_name = {t.name: t for t in TOOL_CATALOG}
    assert by_name["log_wellness"].category == "logging"
    assert by_name["log_medication"].category == "logging"
    assert by_name["get_today_meds"].category == "logging"
    assert by_name["flag_for_followup"].category == "safety"
    assert by_name["schedule_callback"].category == "safety"
    assert by_name["send_sms"].category == "messaging"
    assert by_name["end_call"].category == "lifecycle"
    # end_call is locked-on; send_sms needs >=1 template before it is offered.
    assert by_name["end_call"].always_on is True
    assert by_name["send_sms"].requires_config is True
    # Every other tool keeps the conservative defaults.
    for name, spec in by_name.items():
        if name != "end_call":
            assert spec.always_on is False
        if name != "send_sms":
            assert spec.requires_config is False


def test_tool_names_is_frozenset_of_catalog_names():
    assert isinstance(TOOL_NAMES, frozenset)
    assert TOOL_NAMES == {t.name for t in TOOL_CATALOG}


def test_tool_spec_default_flags():
    spec = ToolSpec(name="x", label="X", description="d", category="logging")
    assert spec.always_on is False
    assert spec.requires_config is False


def test_catalog_response_wraps_tools_list():
    resp = ToolCatalogResponse(tools=list(TOOL_CATALOG))
    assert [t.name for t in resp.tools] == [t.name for t in TOOL_CATALOG]
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_tool_catalog.py -v
```
RED reason: `ModuleNotFoundError: No module named 'usan_api.schemas.tool_catalog'` (the module does not exist yet).

- [ ] Step 3: Implement

```python
# apps/api/src/usan_api/schemas/tool_catalog.py
"""The agent tool catalog (Admin-UI Phase 3 design §4.1).

This module is the AUTHORITATIVE definition of the agent's tool inventory. Unlike
the variable catalog (an open-ended set where unknown names warn-don't-block), the
tool catalog is a CLOSED set: ``schemas/agent_config.ToolsConfig._known_tools``
imports ``TOOL_NAMES`` from here and HARD-BLOCKS unknown tool names. The agent
holds a hand-mirrored ``_TOOL_REGISTRY`` (services/agent/.../check_in.py); the
admin-ui fetches the full list at runtime from GET /v1/admin/tool-catalog. The
catalog is a GLOBAL constant, NOT a per-version snapshot, so it never participates
in the agent_profile_versions forward-compat invariant.
"""

from pydantic import BaseModel


class ToolSpec(BaseModel):
    """One catalog tool: how the editor describes it and how it is gated."""

    name: str  # registry key, e.g. "flag_for_followup"
    label: str  # human label for the UI
    description: str  # what it does (shown in the editor)
    category: str  # "logging" | "lifecycle" | "safety" | "messaging"
    # end_call is locked on (cannot be disabled): it drives the only graceful
    # report->goodbye->delete_room->shutdown path.
    always_on: bool = False
    # send_sms needs >=1 SMS template before the agent offers it to the LLM
    # (an enabled-but-template-less send_sms is a dead tool).
    requires_config: bool = False


# The 7 catalog tools, in catalog/display order (design §4.1). Keep this list and
# the agent-side mirror (services/agent/.../check_in.py _TOOL_REGISTRY) in lockstep.
TOOL_CATALOG: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="log_wellness",
        label="Log wellness",
        description="Record the elder's mood, pain level, and a short note for this call.",
        category="logging",
    ),
    ToolSpec(
        name="log_medication",
        label="Log medication",
        description="Record whether the elder has taken a specific medication.",
        category="logging",
    ),
    ToolSpec(
        name="get_today_meds",
        label="Get today's medications",
        description="Read back the medications the elder is scheduled to take today.",
        category="logging",
    ),
    ToolSpec(
        name="flag_for_followup",
        label="Flag for follow-up",
        description="Raise a safety-escalation flag for a human to review after the call.",
        category="safety",
    ),
    ToolSpec(
        name="schedule_callback",
        label="Schedule callback",
        description="Record a call-back request in the elder's words for a human to action.",
        category="safety",
    ),
    ToolSpec(
        name="send_sms",
        label="Send SMS",
        description="Send an operator-authored, non-PHI templated text after the call.",
        category="messaging",
        requires_config=True,
    ),
    ToolSpec(
        name="end_call",
        label="End call",
        description="End the call gracefully once the check-in is complete.",
        category="lifecycle",
        always_on=True,
    ),
)

# Closed set of known tool names — schemas/agent_config.ToolsConfig imports this and
# hard-blocks anything outside it (design §3.1, §4.1).
TOOL_NAMES: frozenset[str] = frozenset(t.name for t in TOOL_CATALOG)


class ToolCatalogResponse(BaseModel):
    tools: list[ToolSpec]
```

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_tool_catalog.py -v && ruff check src/usan_api/schemas/tool_catalog.py && ruff format src/usan_api/schemas/tool_catalog.py && uv run mypy src/usan_api/schemas/tool_catalog.py
```

- [ ] Step 5: Commit

```bash
git add apps/api/src/usan_api/schemas/tool_catalog.py apps/api/tests/test_tool_catalog.py && git commit -m "feat(api): add Phase 3 tool catalog (ToolSpec + 7-entry TOOL_CATALOG + TOOL_NAMES)"
```

---

### Task A2: API `ToolsConfig` — import `TOOL_NAMES` from catalog (R1) + 7-tool default (R2, F1)

**Files:**
- Modify: `apps/api/src/usan_api/schemas/agent_config.py` (line 17 import block; line 20 `TOOL_NAMES` literal; lines 174–182 `ToolsConfig.enabled` default_factory)
- Test: `apps/api/tests/test_agent_config_schema.py` (lines 20–25 and the `test_legacy_config_still_deserializes` assertion at line 44 — F1)

- [ ] Step 1: Write the failing test (rewrite the two hardcoded 4-tool assertions to the final 7-list, in this exact order, and add a single-source-of-truth assertion)

Edit `apps/api/tests/test_agent_config_schema.py`. Replace the `set(...)` assertion in `test_default_config_matches_current_agent_constants` (currently lines 20–25):

```python
    assert cfg.tools.enabled == [
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "flag_for_followup",
        "schedule_callback",
        "send_sms",
        "end_call",
    ]
```

Add a new test at the end of the file proving the import wiring (R1 — the literal frozenset is gone, `TOOL_NAMES` IS the catalog's):

```python
def test_tools_config_tool_names_is_catalog_single_source():
    from usan_api.schemas.agent_config import TOOL_NAMES as CONFIG_TOOL_NAMES
    from usan_api.schemas.tool_catalog import TOOL_CATALOG, TOOL_NAMES

    assert CONFIG_TOOL_NAMES is TOOL_NAMES
    assert CONFIG_TOOL_NAMES == {t.name for t in TOOL_CATALOG}


def test_tools_accepts_all_seven_catalog_tools():
    from usan_api.schemas.agent_config import ToolsConfig
    from usan_api.schemas.tool_catalog import TOOL_CATALOG

    names = [t.name for t in TOOL_CATALOG]
    assert ToolsConfig(enabled=names).enabled == names
```

(`test_legacy_config_still_deserializes` at line 44 already compares against `DEFAULT_AGENT_CONFIG.tools.enabled` as a set, so it stays green once the default grows — no edit needed there. `test_tools_rejects_unknown_tool` still passes because `launch_missiles` is outside the 7-name catalog.)

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_agent_config_schema.py -v
```
RED reason: `test_default_config_matches_current_agent_constants` fails — `cfg.tools.enabled` is still the old 4-list `["log_wellness","log_medication","get_today_meds","end_call"]`, not the 7-list; and `test_tools_config_tool_names_is_catalog_single_source` fails because `agent_config.TOOL_NAMES` is still the local 4-name literal frozenset (`is`/`==` against the catalog's 7-name set both fail).

- [ ] Step 3: Implement

Edit the import at line 17 of `apps/api/src/usan_api/schemas/agent_config.py` to also pull in `TOOL_NAMES` from the catalog:

```python
from usan_api.schemas.tool_catalog import TOOL_NAMES
from usan_api.schemas.variable_catalog import BUILTIN_NAMES, PHI_BUILTIN_NAMES
```

Delete the line-20 literal `TOOL_NAMES = frozenset(...)` and its comment (lines 19–20), so the block reads:

```python
# Personalization slots allowed in the inbound template (check_in.py rendering).
# Kept for any external code that may import it; no longer used by the validators.
ALLOWED_TEMPLATE_SLOTS = frozenset({"elder_name", "last_check_in_line"})
```

Replace `ToolsConfig.enabled`'s `default_factory` (lines 175–182) with the final 7 in the canonical order (R2):

```python
class ToolsConfig(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: [
            "log_wellness",
            "log_medication",
            "get_today_meds",
            "flag_for_followup",
            "schedule_callback",
            "send_sms",
            "end_call",
        ]
    )
```

(`_known_tools` validator body is unchanged — it already validates against `TOOL_NAMES`, which is now the catalog's frozenset.)

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_agent_config_schema.py -v && ruff check src/usan_api/schemas/agent_config.py && ruff format src/usan_api/schemas/agent_config.py && uv run mypy .
```

- [ ] Step 5: Commit

```bash
git add apps/api/src/usan_api/schemas/agent_config.py apps/api/tests/test_agent_config_schema.py && git commit -m "feat(api): source ToolsConfig.TOOL_NAMES from catalog and default to 7 tools"
```

---

### Task A3: API `admin_tool_catalog` router + register in `main.py`

**Files:**
- Create: `apps/api/src/usan_api/routers/admin_tool_catalog.py`
- Modify: `apps/api/src/usan_api/main.py` (import block lines 15–28; `include_router` block, after line 122 `admin_variable_catalog`)
- Test: `apps/api/tests/test_tool_catalog_api.py`

- [ ] Step 1: Write the failing test (mirror `test_variable_catalog_api.py`; use the cookie-jar `admin_session` fixture exactly — no `headers=`/`cookies=`)

```python
# apps/api/tests/test_tool_catalog_api.py
def test_tool_catalog_requires_admin_session(client):
    # Mirrors the admin plane: no session cookie -> 401.
    r = client.get("/v1/admin/tool-catalog")
    assert r.status_code == 401


def test_tool_catalog_returns_seven_tools_in_order(client, admin_session):
    r = client.get("/v1/admin/tool-catalog")
    assert r.status_code == 200
    body = r.json()
    assert list(body.keys()) == ["tools"]
    assert [t["name"] for t in body["tools"]] == [
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "flag_for_followup",
        "schedule_callback",
        "send_sms",
        "end_call",
    ]


def test_tool_catalog_each_entry_has_contract_shape(client, admin_session):
    tools = client.get("/v1/admin/tool-catalog").json()["tools"]
    for t in tools:
        assert set(t.keys()) == {
            "name",
            "label",
            "description",
            "category",
            "always_on",
            "requires_config",
        }
    by_name = {t["name"]: t for t in tools}
    assert by_name["end_call"]["always_on"] is True
    assert by_name["send_sms"]["requires_config"] is True
    assert by_name["send_sms"]["category"] == "messaging"
    assert by_name["flag_for_followup"]["category"] == "safety"


def test_tool_catalog_response_matches_tool_names(client, admin_session):
    from usan_api.schemas.tool_catalog import TOOL_NAMES

    tools = client.get("/v1/admin/tool-catalog").json()["tools"]
    assert {t["name"] for t in tools} == TOOL_NAMES
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_tool_catalog_api.py -v
```
RED reason: every authenticated case returns `404` (route `/v1/admin/tool-catalog` is not registered yet), so the `200`/shape assertions fail. (`test_tool_catalog_requires_admin_session` may already pass — a 404 is not a 401 — but it will pass for the right reason after Step 3.)

- [ ] Step 3: Implement

```python
# apps/api/src/usan_api/routers/admin_tool_catalog.py
from fastapi import APIRouter, Depends

from usan_api.auth import require_admin_session
from usan_api.schemas.tool_catalog import TOOL_CATALOG, ToolCatalogResponse

router = APIRouter(
    prefix="/v1/admin/tool-catalog",
    tags=["admin-tool-catalog"],
    dependencies=[Depends(require_admin_session)],
)


@router.get("", response_model=ToolCatalogResponse)
async def get_tool_catalog() -> ToolCatalogResponse:
    """Return the global tool catalog for the agent-config editor (design §4.1).

    Admin-session scope, mirroring admin_variable_catalog. The catalog is a global
    constant (a closed, code-backed inventory), not per-version snapshot data; it is
    the single source of truth the frontend uses to render the tool toggles and the
    editor enforces as a hard set (unknown tool names are rejected, not warned).
    """
    return ToolCatalogResponse(tools=list(TOOL_CATALOG))
```

Register in `main.py`. Add `admin_tool_catalog` to the import block (alphabetical, before `admin_users`):

```python
from usan_api.routers import (
    admin_audit,
    admin_elders,
    admin_profiles,
    admin_tool_catalog,
    admin_users,
    admin_variable_catalog,
    auth,
    calls,
    dnc,
    elders,
    runtime,
    tools,
    webhooks,
)
```

Add the `include_router` call right after the `admin_variable_catalog` line (after current line 122):

```python
    app.include_router(admin_variable_catalog.router)
    app.include_router(admin_tool_catalog.router)
```

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_tool_catalog_api.py -v && ruff check . && ruff format . && uv run mypy .
```

- [ ] Step 5: Commit

```bash
git add apps/api/src/usan_api/routers/admin_tool_catalog.py apps/api/src/usan_api/main.py apps/api/tests/test_tool_catalog_api.py && git commit -m "feat(api): add GET /v1/admin/tool-catalog router"
```

---

### Task A4: Agent `ToolsConfig` default — 7-tool list (R2, F1)

**Files:**
- Modify: `services/agent/src/usan_agent/agent_config.py` (lines 52–60 `ToolsConfig.enabled` default_factory)
- Test: `services/agent/tests/test_agent_config.py` (line 13 `test_default_is_complete_and_branded` `tools.enabled ==` assertion — F1)

- [ ] Step 1: Write the failing test — rewrite the line-13 assertion in `test_default_is_complete_and_branded` to the 7-list:

Replace the existing line:
```python
    assert cfg.tools.enabled == ["log_wellness", "log_medication", "get_today_meds", "end_call"]
```
with:
```python
    assert cfg.tools.enabled == [
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "flag_for_followup",
        "schedule_callback",
        "send_sms",
        "end_call",
    ]
```

(`test_roundtrip_dump_then_validate` and `test_parse_ignores_unknown_fields` compare against `DEFAULT_AGENT_CONFIG` itself, so they stay green automatically.)

- [ ] Step 2: Run test, verify it FAILS

```bash
cd services/agent && uv run pytest tests/test_agent_config.py -v
```
RED reason: `test_default_is_complete_and_branded` fails — `cfg.tools.enabled` is still the old 4-list, not the 7-list.

- [ ] Step 3: Implement — set the agent-side `ToolsConfig.enabled` default_factory to the final 7 (R2). No `sms` field (that is Part D):

```python
class ToolsConfig(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: [
            "log_wellness",
            "log_medication",
            "get_today_meds",
            "flag_for_followup",
            "schedule_callback",
            "send_sms",
            "end_call",
        ]
    )
```

- [ ] Step 4: Run test, verify PASS

```bash
cd services/agent && uv run pytest tests/test_agent_config.py -v && ruff check . && uv run mypy .
```

- [ ] Step 5: Commit

```bash
git add services/agent/src/usan_agent/agent_config.py services/agent/tests/test_agent_config.py && git commit -m "feat(agent): default ToolsConfig.enabled to the 7-tool catalog list"
```

---

### Task A5: Agent `_select_tools` rewrite (R3) + builders call `_select_tools(cfg.tools)`

**Files:**
- Modify: `services/agent/src/usan_agent/check_in.py` (lines 203–213 `_select_tools`; line 239 `build_check_in_agent` call; line 270 `build_inbound_agent` call)
- Test: `services/agent/tests/test_check_in.py` (existing `test_select_tools_filters_and_preserves_order` line 209, `test_select_tools_ignores_unknown_names` line 216, `test_build_check_in_agent_respects_enabled` line 224 — these pass `list`/`enabled`-shaped input and MUST be updated to the new `tools`-object signature)

- [ ] Step 1: Write the failing test — `_select_tools` now takes a `ToolsConfig`-like object (with `.enabled` and optional `.sms`), not a bare list. Update the three existing call-sites and add the send_sms-guard coverage. In `services/agent/tests/test_check_in.py`:

Add a tiny helper near the top (after `_NOW`, line 9):

```python
from types import SimpleNamespace


def _tools(enabled, sms=None):
    # _select_tools takes a ToolsConfig-like object (.enabled + optional .sms).
    return SimpleNamespace(enabled=list(enabled), sms=sms)
```

Replace `test_select_tools_filters_and_preserves_order` (lines 209–213):

```python
def test_select_tools_filters_and_preserves_order():
    tools = check_in._select_tools(_tools(["get_today_meds", "log_wellness"]))
    ids = [t.id for t in tools]
    # order preserved, end_call force-appended for call-termination safety
    assert ids == ["get_today_meds", "log_wellness", "end_call"]
```

Replace `test_select_tools_ignores_unknown_names` (lines 216–221):

```python
def test_select_tools_ignores_unknown_names():
    tools = check_in._select_tools(_tools(["log_wellness", "nonexistent"]))
    ids = {t.id for t in tools}
    assert "nonexistent" not in ids
    assert "log_wellness" in ids
    assert "end_call" in ids
```

Replace `test_build_check_in_agent_respects_enabled` (lines 224–229) — note `send_sms` is NOT yet in `_TOOL_REGISTRY` in Part A, so a default config (which enables `send_sms`) simply won't expose it; assert the registry-backed subset:

```python
def test_build_check_in_agent_respects_enabled():
    cfg = AgentConfig.model_validate(
        {**DEFAULT_AGENT_CONFIG.model_dump(), "tools": {"enabled": ["log_wellness"]}}
    )
    agent = check_in.build_check_in_agent(cfg)
    assert {t.id for t in agent.tools} == {"log_wellness", "end_call"}
```

Add two new tests proving the `send_sms` getattr-guard (R3) is in force even before the registry/`sms` field exist:

```python
def test_select_tools_drops_send_sms_without_templates():
    # send_sms is enabled but has no templates -> not offered (dead tool guard).
    # send_sms is not in _TOOL_REGISTRY in Part A, so this also exercises the
    # registry filter; either way send_sms must be absent.
    sms_cfg = SimpleNamespace(templates=[])
    tools = check_in._select_tools(_tools(["log_wellness", "send_sms"], sms=sms_cfg))
    ids = {t.id for t in tools}
    assert "send_sms" not in ids
    assert ids == {"log_wellness", "end_call"}


def test_select_tools_safe_when_tools_has_no_sms_attr():
    # ToolsConfig in Part A has no `sms` field; the getattr guard must not raise.
    tools = check_in._select_tools(_tools(["log_wellness"]))
    assert {t.id for t in tools} == {"log_wellness", "end_call"}
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd services/agent && uv run pytest tests/test_check_in.py -k select_tools -v
```
RED reason: the current `_select_tools(enabled: list[str])` iterates `enabled` directly; passing a `SimpleNamespace` makes `for n in enabled` iterate the namespace (raising `TypeError`/yielding no tool names), so `test_select_tools_filters_and_preserves_order` and the new guard tests fail.

- [ ] Step 3: Implement — replace `_select_tools` VERBATIM per R3, and update both builders to call `_select_tools(cfg.tools)`.

Replace lines 203–213 (`_select_tools`):

```python
def _select_tools(tools: "ToolsConfig") -> list[Any]:
    names = [n for n in tools.enabled if n in _TOOL_REGISTRY]  # preserve enabled order
    sms_cfg = getattr(tools, "sms", None)
    if not (sms_cfg and getattr(sms_cfg, "templates", None)):
        names = [n for n in names if n != "send_sms"]
    if "end_call" not in names:
        names.append("end_call")
    return [_TOOL_REGISTRY[n] for n in names]
```

Add `ToolsConfig` to the existing `agent_config` import (line 18) so the annotation resolves under `mypy`:

```python
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig, ToolsConfig
```

In `build_check_in_agent`, change line 239:
```python
        tools=_select_tools(cfg.tools),
```

In `build_inbound_agent`, change line 270:
```python
        tools=_select_tools(cfg.tools),
```

- [ ] Step 4: Run test, verify PASS

```bash
cd services/agent && uv run pytest tests/test_check_in.py -k select_tools -v && ruff check . && uv run mypy .
```

- [ ] Step 5: Commit

```bash
git add services/agent/src/usan_agent/check_in.py services/agent/tests/test_check_in.py && git commit -m "refactor(agent): _select_tools takes ToolsConfig with send_sms template guard"
```

---

### Task A6: Agent — rewrite the two four-tool builder tests to be registry-driven (R4)

**Files:**
- Test: `services/agent/tests/test_check_in.py` (`test_build_check_in_agent_attaches_four_tools` lines 145–150; `test_build_inbound_agent_has_same_four_tools` lines 153–157)

This task RE-WRITES these two tests ONCE so they recompute their expectation from `_TOOL_REGISTRY` and survive B/C/D growing the registry (R4). The existing tests read tool identity via `t.id` (livekit-agents 1.5.14: `FunctionTool` has no `.name`, `.id == function name`), so the rewrite preserves that introspection.

- [ ] Step 1: Write the failing test — replace both tests:

```python
def test_build_check_in_agent_attaches_registry_tools():
    agent = check_in.build_check_in_agent()
    # Registry-driven so it survives Parts B/C/D growing _TOOL_REGISTRY: the default
    # config enables all 7 catalog tools, but only those present in the registry are
    # attachable, and send_sms is excluded without templates; end_call is always on.
    # livekit-agents 1.5.14: FunctionTool has no .name; use .id (== function name).
    expected = {
        n
        for n in DEFAULT_AGENT_CONFIG.tools.enabled
        if n in check_in._TOOL_REGISTRY and n != "send_sms"
    } | {"end_call"}
    assert {t.id for t in agent.tools} == expected
    assert agent.instructions == check_in.CHECK_IN_INSTRUCTIONS


def test_build_inbound_agent_attaches_same_registry_tools():
    agent = check_in.build_inbound_agent(None, resolved_vars={"elder_name": "Ada"}, now=_NOW)
    expected = {
        n
        for n in DEFAULT_AGENT_CONFIG.tools.enabled
        if n in check_in._TOOL_REGISTRY and n != "send_sms"
    } | {"end_call"}
    assert {t.id for t in agent.tools} == expected
    assert "Ada" in agent.instructions
```

- [ ] Step 2: Run test, verify it FAILS (then PASSES) — these tests are a refactor of green tests; run them to confirm they evaluate correctly against the current Part-A registry (only `log_wellness`/`log_medication`/`get_today_meds`/`end_call` are registered, `flag_for_followup`/`schedule_callback`/`send_sms` are filtered out → `expected == {log_wellness, log_medication, get_today_meds, end_call}`):

```bash
cd services/agent && uv run pytest tests/test_check_in.py -k registry_tools -v
```
RED guard: if the rewrite is mis-typed (e.g. a hardcoded count slips in, or `_TOOL_REGISTRY` is referenced before import), the test errors. Expected after a correct rewrite: PASS. (The original 4-tool literal-set tests would have started FAILING in B/C once the registry grows — that is exactly why R4 mandates this registry-driven rewrite now.)

- [ ] Step 3: Implement — none (this task only edits tests; the production behavior is already correct from A5). Confirm no `check_in.py` change is needed.

- [ ] Step 4: Run the full agent suite, verify PASS

```bash
cd services/agent && uv run pytest tests/test_check_in.py -v && ruff check . && uv run mypy .
```

- [ ] Step 5: Commit

```bash
git add services/agent/tests/test_check_in.py && git commit -m "test(agent): make builder tool-count tests registry-driven (survive B/C/D)"
```

---

### Task A7: Admin-UI — `toolCatalog.ts` + `useToolCatalog` (clone of `variableCatalog.ts`)

**Files:**
- Create: `apps/admin-ui/src/config/toolCatalog.ts`
- Test: `apps/admin-ui/src/test/toolCatalog.test.tsx`

- [ ] Step 1: Write the failing test (mirror `variableCatalog.test.tsx`)

```tsx
// apps/admin-ui/src/test/toolCatalog.test.tsx
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { useToolCatalog, type ToolSpec } from "../config/toolCatalog";

const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (u: string) => getMock(u) },
}));

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
}

const SAMPLE: ToolSpec[] = [
  {
    name: "log_wellness",
    label: "Log wellness",
    description: "Record the elder's wellness.",
    category: "logging",
    always_on: false,
    requires_config: false,
  },
  {
    name: "end_call",
    label: "End call",
    description: "End the call gracefully.",
    category: "lifecycle",
    always_on: true,
    requires_config: false,
  },
];

afterEach(() => {
  vi.restoreAllMocks();
  getMock.mockReset();
});

describe("useToolCatalog", () => {
  it("fetches /v1/admin/tool-catalog and returns the tools", async () => {
    getMock.mockResolvedValue({ tools: SAMPLE });
    const { result } = renderHook(() => useToolCatalog(), { wrapper: wrapper() });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(getMock).toHaveBeenCalledWith("/v1/admin/tool-catalog");
    expect(result.current.data).toEqual(SAMPLE);
  });
});
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/admin-ui && npm test -- src/test/toolCatalog.test.tsx
```
RED reason: `Failed to resolve import "../config/toolCatalog"` (the module does not exist yet).

- [ ] Step 3: Implement

```ts
// apps/admin-ui/src/config/toolCatalog.ts
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";

// Mirrors apps/api/src/usan_api/schemas/tool_catalog.py (ToolSpec). The API is
// authoritative; the frontend fetches the catalog at runtime so the tool toggles
// never hand-duplicate the inventory. Unlike the variable catalog, this is a CLOSED
// set: enabling a name outside it is a hard validation error server-side.
export interface ToolSpec {
  name: string;
  label: string;
  description: string;
  category: string; // "logging" | "lifecycle" | "safety" | "messaging"
  // end_call is locked on (rendered, cannot be disabled).
  always_on: boolean;
  // send_sms needs >=1 template before the agent offers it.
  requires_config: boolean;
}

interface ToolCatalogResponse {
  tools: ToolSpec[];
}

// Catalog is a global constant on the server (not per-version), so it is highly
// cacheable. Long staleTime avoids refetching it on every editor mount.
const CATALOG_KEY = ["tool-catalog"] as const;

export function useToolCatalog() {
  return useQuery<ToolSpec[]>({
    queryKey: CATALOG_KEY,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const res = await api.get<ToolCatalogResponse>("/v1/admin/tool-catalog");
      return res.tools;
    },
  });
}
```

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/admin-ui && npm test -- src/test/toolCatalog.test.tsx && npm run lint && npx tsc --noEmit
```

- [ ] Step 5: Commit

```bash
git add apps/admin-ui/src/config/toolCatalog.ts apps/admin-ui/src/test/toolCatalog.test.tsx && git commit -m "feat(admin-ui): add useToolCatalog hook + ToolSpec type"
```

---

### Task A8: Admin-UI — widen `TOOL_NAMES`/`ToolName` to 7 + de-hardcode `TOOL_HELP` in `ToolsSection` (F2)

Per F2, the `ToolsSection.tsx` rewrite that REMOVES the hardcoded `TOOL_HELP: Record<ToolName, string>` must land in the SAME task as widening `TOOL_NAMES`/`ToolName` to 7 — otherwise widening `ToolName` turns the 4-key `TOOL_HELP` into a `TS2741` error and the `tsc --noEmit` gate fails. This single task does both, catalog-driven.

**Files:**
- Modify: `apps/admin-ui/src/config/agentConfigSchema.ts` (line 7 `TOOL_NAMES`)
- Modify: `apps/admin-ui/src/features/editor/sections/ToolsSection.tsx` (full rewrite — drop `TOOL_HELP`, render from catalog, lock `end_call`)
- Test: `apps/admin-ui/src/test/ToolsSection.test.tsx` (new); `apps/admin-ui/src/test/agentConfigSchema.test.ts` (line 26 — the default-tools assertion needs the 7-list so the parse stays valid)

- [ ] Step 1: Write the failing tests

First, update the existing `agentConfigSchema.test.ts` line-26 fixture to the 7-list so it remains a valid config under the widened enum (it currently uses the 4-list; keep it parsing). Replace:
```ts
    tools: { enabled: ["log_wellness", "log_medication", "get_today_meds", "end_call"] },
```
with:
```ts
    tools: {
      enabled: [
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "flag_for_followup",
        "schedule_callback",
        "send_sms",
        "end_call",
      ],
    },
```

Add a `TOOL_NAMES` width assertion to `agentConfigSchema.test.ts`:

```ts
it("exposes all seven catalog tool names", () => {
  expect([...TOOL_NAMES]).toEqual([
    "log_wellness",
    "log_medication",
    "get_today_meds",
    "flag_for_followup",
    "schedule_callback",
    "send_sms",
    "end_call",
  ]);
});
```
(add `TOOL_NAMES` to the existing `import { ... } from "../config/agentConfigSchema"` line at the top of that test file.)

New `ToolsSection.test.tsx` — render catalog-driven toggles, `end_call` locked, descriptions from catalog:

```tsx
// apps/admin-ui/src/test/ToolsSection.test.tsx
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import type { ReactNode } from "react";
import { ToolsSection } from "../features/editor/sections/ToolsSection";
import type { AgentConfigForm } from "../config/agentConfigSchema";
import type { ToolSpec } from "../config/toolCatalog";

const getMock = vi.fn();
vi.mock("../lib/api", () => ({
  api: { get: (u: string) => getMock(u) },
}));

const CATALOG: ToolSpec[] = [
  {
    name: "log_wellness",
    label: "Log wellness",
    description: "Record the elder's wellness.",
    category: "logging",
    always_on: false,
    requires_config: false,
  },
  {
    name: "send_sms",
    label: "Send SMS",
    description: "Send a templated text after the call.",
    category: "messaging",
    always_on: false,
    requires_config: true,
  },
  {
    name: "end_call",
    label: "End call",
    description: "End the call gracefully.",
    category: "lifecycle",
    always_on: true,
    requires_config: false,
  },
];

function Harness({ enabled }: { enabled: string[] }) {
  const form = useForm<AgentConfigForm>({
    defaultValues: { tools: { enabled } } as AgentConfigForm,
  });
  return <ToolsSection form={form} />;
}

function wrapper(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

afterEach(() => {
  vi.restoreAllMocks();
  getMock.mockReset();
});

describe("ToolsSection", () => {
  it("renders a row + description for every catalog tool", async () => {
    getMock.mockResolvedValue({ tools: CATALOG });
    render(wrapper(<Harness enabled={["log_wellness", "end_call"]} />));

    expect(await screen.findByText("Record the elder's wellness.")).toBeInTheDocument();
    expect(screen.getByText("Send a templated text after the call.")).toBeInTheDocument();
    expect(screen.getByText("End the call gracefully.")).toBeInTheDocument();
  });

  it("renders end_call locked-on (checked + disabled)", async () => {
    getMock.mockResolvedValue({ tools: CATALOG });
    render(wrapper(<Harness enabled={["end_call"]} />));

    const endCall = (await screen.findByLabelText(/end_call/i)) as HTMLInputElement;
    expect(endCall.checked).toBe(true);
    expect(endCall.disabled).toBe(true);
  });
});
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/admin-ui && npm test -- src/test/ToolsSection.test.tsx src/test/agentConfigSchema.test.ts && npx tsc --noEmit
```
RED reason: `ToolsSection` is still hardcoded to the 4-name `TOOL_NAMES` + `TOOL_HELP`, so the catalog descriptions never render (`findByText` times out) and `end_call` is not rendered disabled. `agentConfigSchema.test.ts` fails the new `TOOL_NAMES` width assertion (only 4 names today). Once `ToolName` is widened in Step 3, leaving the old `TOOL_HELP: Record<ToolName,string>` (4 keys) in place would additionally make `tsc --noEmit` fail with `TS2741` — which is why both edits ship here together (F2).

- [ ] Step 3: Implement

Widen `TOOL_NAMES` in `agentConfigSchema.ts` (line 7):

```ts
// Tool names the agent can register (TOOL_NAMES). Closed set, mirrors
// apps/api schemas/tool_catalog.TOOL_NAMES — enabling a name outside it is rejected.
export const TOOL_NAMES = [
  "log_wellness",
  "log_medication",
  "get_today_meds",
  "flag_for_followup",
  "schedule_callback",
  "send_sms",
  "end_call",
] as const;
export type ToolName = (typeof TOOL_NAMES)[number];
```

Rewrite `ToolsSection.tsx` — drop `TOOL_HELP`, render toggles/descriptions from `useToolCatalog()`, lock `end_call` (`always_on`), preserve canonical `TOOL_NAMES` order for stable diffs:

```tsx
import { Controller, type UseFormReturn } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";
import { TOOL_NAMES, type ToolName } from "../../../config/agentConfigSchema";
import { useToolCatalog, type ToolSpec } from "../../../config/toolCatalog";

// Catalog-driven tool list (Phase 3 §4.3): toggles + descriptions render from the
// server's TOOL_CATALOG (fetched via useToolCatalog), so the inventory is never
// hand-duplicated. end_call is locked on (always_on); send_sms shows a hint when it
// is enabled but has no templates yet (the SMS templates editor lands in Part D).
export function ToolsSection({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const { data: catalog } = useToolCatalog();
  const error = form.formState.errors.tools?.enabled?.message;
  const specByName = new Map<string, ToolSpec>((catalog ?? []).map((s) => [s.name, s]));
  return (
    <div className="space-y-3">
      <p className="text-sm text-slate-500">Functions the agent can call during a call.</p>
      <Controller
        control={form.control}
        name="tools.enabled"
        render={({ field }) => {
          const enabled = new Set(field.value);
          function toggle(tool: ToolName, on: boolean): void {
            const next = new Set(enabled);
            if (on) next.add(tool);
            else next.delete(tool);
            // Preserve canonical order so diffs stay stable.
            field.onChange(TOOL_NAMES.filter((t) => next.has(t)));
          }
          return (
            <ul className="space-y-2">
              {TOOL_NAMES.map((tool) => {
                const spec = specByName.get(tool);
                const lockedOn = spec?.always_on === true;
                const description = spec?.description ?? "";
                return (
                  <li
                    key={tool}
                    className="flex items-start justify-between gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-card"
                  >
                    <label htmlFor={`tool-${tool}`} className="min-w-0">
                      <span className="font-mono text-sm text-slate-900">{tool}</span>
                      <span className="mt-0.5 block text-xs text-slate-500">{description}</span>
                    </label>
                    <input
                      id={`tool-${tool}`}
                      type="checkbox"
                      className="mt-1 h-4 w-4 accent-indigo-600"
                      checked={lockedOn ? true : enabled.has(tool)}
                      disabled={lockedOn}
                      onChange={(e) => toggle(tool, e.target.checked)}
                    />
                  </li>
                );
              })}
            </ul>
          );
        }}
      />
      {error ? <p className="text-xs font-medium text-red-700">{error}</p> : null}
    </div>
  );
}
```

(`ProfileEditorPage.tsx` renders `<ToolsSection form={form} />` and is already inside the app's `QueryClientProvider`, so no call-site change is needed. `ProfileEditorPage.test.tsx` only sets `tools.enabled: ["log_wellness","end_call"]`, which is still valid under the widened enum.)

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/admin-ui && npm test -- src/test/ToolsSection.test.tsx src/test/agentConfigSchema.test.ts src/test/ProfileEditorPage.test.tsx && npm run lint && npx tsc --noEmit
```

- [ ] Step 5: Commit

```bash
git add apps/admin-ui/src/config/agentConfigSchema.ts apps/admin-ui/src/features/editor/sections/ToolsSection.tsx apps/admin-ui/src/test/ToolsSection.test.tsx apps/admin-ui/src/test/agentConfigSchema.test.ts && git commit -m "feat(admin-ui): catalog-driven ToolsSection + widen TOOL_NAMES to 7"
```

---

### Task A9: Admin-UI — de-hardcode `tools.enabled` field-meta help

**Files:**
- Modify: `apps/admin-ui/src/config/fieldMeta.ts` (lines 95–98 `"tools.enabled"`)
- Test: `apps/admin-ui/src/test/fieldMeta.test.ts` (add an assertion)

- [ ] Step 1: Write the failing test — add to `fieldMeta.test.ts`:

```ts
describe("fieldMeta tools help text", () => {
  it("does not hardcode the old four-tool list", () => {
    const help = fieldMeta["tools.enabled"]!.help;
    // The catalog is now the source of truth; help must not enumerate the old set.
    expect(help).not.toContain("log_medication");
    expect(help).not.toContain("get_today_meds");
    expect(help.toLowerCase()).toContain("catalog");
  });
});
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/admin-ui && npm test -- src/test/fieldMeta.test.ts
```
RED reason: current `tools.enabled` help is `"Subset of log_wellness, log_medication, get_today_meds, end_call."` — it contains `log_medication`/`get_today_meds` and no `"catalog"`, so all three assertions fail.

- [ ] Step 3: Implement — replace the `tools.enabled` entry (lines 95–98):

```ts
  // Tools
  "tools.enabled": {
    label: "Enabled tools",
    help: "Which tools the agent may call this profile. The available tools come from the server catalog; end_call is always on.",
  },
```

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/admin-ui && npm test -- src/test/fieldMeta.test.ts && npm run lint && npx tsc --noEmit
```

- [ ] Step 5: Commit

```bash
git add apps/admin-ui/src/config/fieldMeta.ts apps/admin-ui/src/test/fieldMeta.test.ts && git commit -m "feat(admin-ui): de-hardcode tools.enabled field-meta help"
```

---

### Task A10: Part A full-suite gate (all three layers green)

**Files:** none (verification only).

- [ ] Step 1: N/A (no new test).
- [ ] Step 2: N/A.
- [ ] Step 3: N/A.
- [ ] Step 4: Run every layer's full gate and confirm green before handing off to Part B:

```bash
cd apps/api && uv run pytest -v && ruff check . && ruff format --check . && uv run mypy .
cd ../../services/agent && uv run pytest -v && ruff check . && uv run mypy .
cd ../../apps/admin-ui && npm test && npm run lint && npx tsc --noEmit
```
All three must pass. This proves the shared-surface mutations (catalog, `TOOL_NAMES` rewire, 7-tool defaults, `_select_tools`, registry-driven builder tests, catalog-driven UI) leave the repo green so Part B can assume Part A landed.

- [ ] Step 5: Commit — nothing to commit (verification only). If `ruff format` rewrote anything, `git add -A && git commit -m "chore(api,agent): ruff format Part A"`.

---

## Notes for downstream parts (ownership, do NOT violate)
- **R1:** `apps/api` `TOOL_NAMES` literal is deleted in A2 and now imported from `tool_catalog`. B/C/D must NOT touch `TOOL_NAMES`.
- **R2:** Both `ToolsConfig.enabled` default_factories (api A2, agent A4) are set ONCE to the final 7. B/C/D must NOT edit `default_factory`.
- **R3:** `_select_tools(tools)` is written VERBATIM in A5 with the `getattr(tools, "sms", None)` guard — safe before the agent `ToolsConfig` gains `sms` in Part D. B/C/D must NOT redefine it; Part D adds `sms` to the agent `ToolsConfig` but does NOT touch `_select_tools`.
- **R4:** The two builder tests are rewritten registry-driven in A6 (`expected` recomputes from `_TOOL_REGISTRY`). B/C/D must NOT touch them; they stay green as the registry grows.
- **F1:** Both four-tool default test assertions (api `test_agent_config_schema` line 20, agent `test_agent_config` line 13) were rewritten to the 7-list in the SAME tasks (A2, A4) that changed the defaults.
- **F2:** `ToolsSection` `TOOL_HELP` removal + `TOOL_NAMES`/`ToolName` widening shipped together in A8 (no intermediate `TS2741`).
- Part A does NOT add the catalog↔registry sync test (Part D final, R8), nor any of the 3 new tools' endpoints/`@function_tool`/`_do_*`/`api_client` functions/repos/migration (B/C/D).

## Files read (for reference)
- Spec: `/Users/evgenii.vasilenko/gofrolist/usan-voice-engine/docs/superpowers/specs/2026-06-09-admin-ui-phase3-tools-design.md`
- `/Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api/src/usan_api/schemas/variable_catalog.py`
- `/Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api/src/usan_api/routers/admin_variable_catalog.py`
- `/Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api/src/usan_api/schemas/agent_config.py` (TOOL_NAMES line 20, ToolsConfig lines 174–190)
- `/Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api/src/usan_api/main.py` (imports 15–28, include_router 118–129)
- `/Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/api/tests/test_agent_config_schema.py`, `test_variable_catalog_api.py`, `conftest.py` (admin_session fixture line 165, sets cookie on shared client)
- `/Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent/src/usan_agent/check_in.py` (_TOOL_REGISTRY 195–200, _select_tools 203–213, builders 239/270)
- `/Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent/src/usan_agent/agent_config.py` (ToolsConfig 52–60)
- `/Users/evgenii.vasilenko/gofrolist/usan-voice-engine/services/agent/tests/test_check_in.py` (four-tool tests 145–157, uses `t.id`), `test_agent_config.py` (line 13)
- `/Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/admin-ui/src/config/variableCatalog.ts`, `agentConfigSchema.ts` (TOOL_NAMES line 7), `fieldMeta.ts` (tools.enabled 95–98)
- `/Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/admin-ui/src/features/editor/sections/ToolsSection.tsx` (hardcoded TOOL_HELP 5–10), `ProfileEditorPage.tsx` (renders `<ToolsSection form={form}/>` line 199)
- `/Users/evgenii.vasilenko/gofrolist/usan-voice-engine/apps/admin-ui/src/test/variableCatalog.test.tsx`, `fieldMeta.test.ts`, `agentConfigSchema.test.ts` (line 26), `ProfileEditorPage.test.tsx`

---

## Part B — migration+models (shared) + flag_for_followup (additive only)

### Task B1: Migration 0011 — follow_up_flags, callback_requests, sms_messages (all 3 tables)

**Files:**
- Create: `apps/api/migrations/versions/0011_followup_callback_sms.py`
- Test: `apps/api/tests/test_phase3_migration.py`

- [ ] Step 1: Write the failing test

```python
# apps/api/tests/test_phase3_migration.py
"""Migration 0011 creates the three Phase-3 tool tables with the right shape.

Runs against the testcontainers Postgres that conftest's `database_url` already
migrated to head. We introspect the live catalog so the test fails before 0011
exists (tables absent) and passes once it lands.
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def _columns(async_database_url: str, table: str) -> dict[str, str]:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = :t ORDER BY ordinal_position"
                ),
                {"t": table},
            )
            return {r[0]: r[1] for r in rows}
    finally:
        await engine.dispose()


async def _indexes(async_database_url: str, table: str) -> set[str]:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            rows = await conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = :t"),
                {"t": table},
            )
            return {r[0] for r in rows}
    finally:
        await engine.dispose()


def test_follow_up_flags_table_shape(async_database_url):
    cols = asyncio.run(_columns(async_database_url, "follow_up_flags"))
    assert cols["id"] == "bigint"
    assert cols["call_id"] == "uuid"
    assert cols["elder_id"] == "uuid"
    assert cols["severity"] == "text"
    assert cols["category"] == "text"
    assert cols["reason"] == "text"
    assert cols["status"] == "text"
    assert cols["created_at"] == "timestamp with time zone"
    idx = asyncio.run(_indexes(async_database_url, "follow_up_flags"))
    assert "idx_followup_flags_elder" in idx
    assert "idx_followup_flags_status" in idx


def test_callback_requests_table_shape(async_database_url):
    cols = asyncio.run(_columns(async_database_url, "callback_requests"))
    assert cols["id"] == "bigint"
    assert cols["call_id"] == "uuid"
    assert cols["elder_id"] == "uuid"
    assert cols["requested_time_text"] == "text"
    assert cols["requested_at"] == "timestamp with time zone"
    assert cols["notes"] == "text"
    assert cols["status"] == "text"
    assert cols["created_at"] == "timestamp with time zone"
    idx = asyncio.run(_indexes(async_database_url, "callback_requests"))
    assert "idx_callback_requests_status" in idx


def test_sms_messages_table_shape(async_database_url):
    cols = asyncio.run(_columns(async_database_url, "sms_messages"))
    assert cols["id"] == "uuid"
    assert cols["call_id"] == "uuid"
    assert cols["elder_id"] == "uuid"
    assert cols["to_number"] == "text"
    assert cols["template_key"] == "text"
    assert cols["body"] == "text"
    assert cols["status"] == "text"
    assert cols["telnyx_message_id"] == "text"
    assert cols["error"] == "jsonb"
    assert cols["sent_at"] == "timestamp with time zone"
    assert cols["created_at"] == "timestamp with time zone"
    assert cols["updated_at"] == "timestamp with time zone"
    idx = asyncio.run(_indexes(async_database_url, "sms_messages"))
    assert "idx_sms_messages_call" in idx
    assert "idx_sms_messages_status" in idx


def test_follow_up_flags_cascades_call_not_elder(async_database_url):
    # CASCADE to calls(id), NO cascade to elders(id): the FK delete rules must differ.
    engine = create_async_engine(async_database_url, poolclass=NullPool)

    async def _rules() -> dict[str, str]:
        async with engine.begin() as conn:
            rows = await conn.execute(
                text(
                    "SELECT ccu.table_name AS ref_table, rc.delete_rule "
                    "FROM information_schema.referential_constraints rc "
                    "JOIN information_schema.constraint_column_usage ccu "
                    "  ON ccu.constraint_name = rc.constraint_name "
                    "JOIN information_schema.table_constraints tc "
                    "  ON tc.constraint_name = rc.constraint_name "
                    "WHERE tc.table_name = 'follow_up_flags'"
                )
            )
            return {r[0]: r[1] for r in rows}

    try:
        rules = asyncio.run(_rules())
    finally:
        asyncio.run(engine.dispose())
    assert rules["calls"] == "CASCADE"
    assert rules["elders"] == "NO ACTION"
```

- [ ] Step 2: Run test, verify it FAILS
  - `cd apps/api && uv run pytest tests/test_phase3_migration.py -v`
  - RED reason: conftest runs `alembic upgrade head`, which stops at 0010; tables `follow_up_flags`/`callback_requests`/`sms_messages` do not exist, so `_columns` returns `{}` and every `cols["id"]` raises `KeyError`.

- [ ] Step 3: Implement

```python
# apps/api/migrations/versions/0011_followup_callback_sms.py
"""phase-3 tool tables: follow_up_flags, callback_requests, sms_messages

Revision ID: 0011
Revises: 0010
Create Date: 2026-06-09

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # follow_up_flags: human-reviewed safety escalations (spec §5.1).
    # CASCADE to calls (a deleted call drops its flags); NO cascade to elders
    # (an elder delete must not silently erase clinical follow-up history).
    op.execute(
        """
        CREATE TABLE follow_up_flags (
            id          BIGSERIAL PRIMARY KEY,
            call_id     UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            elder_id    UUID NOT NULL REFERENCES elders(id),
            severity    TEXT NOT NULL,
            category    TEXT NOT NULL,
            reason      TEXT,
            status      TEXT NOT NULL DEFAULT 'open',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_followup_flags_elder "
        "ON follow_up_flags(elder_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX idx_followup_flags_status "
        "ON follow_up_flags(status, created_at DESC)"
    )

    # callback_requests: durable call-back asks for a human to action (spec §5.2).
    op.execute(
        """
        CREATE TABLE callback_requests (
            id                  BIGSERIAL PRIMARY KEY,
            call_id             UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            elder_id            UUID NOT NULL REFERENCES elders(id),
            requested_time_text TEXT NOT NULL,
            requested_at        TIMESTAMPTZ,
            notes               TEXT,
            status              TEXT NOT NULL DEFAULT 'open',
            created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX idx_callback_requests_status "
        "ON callback_requests(status, created_at DESC)"
    )

    # sms_messages: queued outbound texts, flushed post-call (spec §6.4).
    op.execute(
        """
        CREATE TABLE sms_messages (
            id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            call_id           UUID NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
            elder_id          UUID NOT NULL REFERENCES elders(id),
            to_number         TEXT NOT NULL,
            template_key      TEXT NOT NULL,
            body              TEXT NOT NULL,
            status            TEXT NOT NULL DEFAULT 'pending',
            telnyx_message_id TEXT UNIQUE,
            error             JSONB,
            sent_at           TIMESTAMPTZ,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_sms_messages_call ON sms_messages(call_id, status)")
    op.execute("CREATE INDEX idx_sms_messages_status ON sms_messages(status, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sms_messages")
    op.execute("DROP TABLE IF EXISTS callback_requests")
    op.execute("DROP TABLE IF EXISTS follow_up_flags")
```

- [ ] Step 4: Run test, verify PASS
  - `cd apps/api && uv run pytest tests/test_phase3_migration.py -v` (the session-scoped `database_url` fixture re-runs `alembic upgrade head`, which now applies 0011)

- [ ] Step 5: Commit
  - `git add apps/api/migrations/versions/0011_followup_callback_sms.py apps/api/tests/test_phase3_migration.py && git commit -m "feat(api): migration 0011 — follow_up_flags, callback_requests, sms_messages tables"`

---

### Task B2: ORM models — FollowUpFlag, CallbackRequest, SmsMessage

**Files:**
- Modify: `apps/api/src/usan_api/db/models.py` (append after `AdminAuditLog`, end of file ~line 346)
- Test: `apps/api/tests/test_phase3_models.py`

- [ ] Step 1: Write the failing test

```python
# apps/api/tests/test_phase3_models.py
"""The three Phase-3 ORM models mirror the migration 0011 schema."""

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from usan_api.db.models import CallbackRequest, FollowUpFlag, SmsMessage


def test_follow_up_flag_columns_and_table():
    assert FollowUpFlag.__tablename__ == "follow_up_flags"
    cols = FollowUpFlag.__table__.columns
    assert {"id", "call_id", "elder_id", "severity", "category", "reason",
            "status", "created_at"} <= set(cols.keys())
    assert cols["call_id"].foreign_keys.pop().ondelete == "CASCADE"
    assert not cols["severity"].nullable
    assert not cols["status"].nullable


def test_callback_request_columns_and_table():
    assert CallbackRequest.__tablename__ == "callback_requests"
    cols = CallbackRequest.__table__.columns
    assert {"id", "call_id", "elder_id", "requested_time_text", "requested_at",
            "notes", "status", "created_at"} <= set(cols.keys())
    assert not cols["requested_time_text"].nullable
    assert cols["requested_at"].nullable


def test_sms_message_columns_uuid_pk_and_defaults():
    assert SmsMessage.__tablename__ == "sms_messages"
    cols = SmsMessage.__table__.columns
    assert {"id", "call_id", "elder_id", "to_number", "template_key", "body",
            "status", "telnyx_message_id", "error", "sent_at", "created_at",
            "updated_at"} <= set(cols.keys())
    assert isinstance(cols["id"].type, UUID)
    assert isinstance(cols["error"].type, JSONB)
    assert cols["telnyx_message_id"].unique is True
    # UUID server default + updated_at onupdate (mirrors Call/Elder style).
    assert "gen_random_uuid" in str(cols["id"].server_default.arg)
    assert cols["updated_at"].onupdate is not None
```

- [ ] Step 2: Run test, verify it FAILS
  - `cd apps/api && uv run pytest tests/test_phase3_models.py -v`
  - RED reason: `from usan_api.db.models import CallbackRequest, FollowUpFlag, SmsMessage` raises `ImportError` — none of the three classes exist yet.

- [ ] Step 3: Implement (append to `apps/api/src/usan_api/db/models.py` after the `AdminAuditLog` class, the current last class at line 332-345)

```python


class FollowUpFlag(Base):
    __tablename__ = "follow_up_flags"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    # No ondelete on elders: a follow-up flag's clinical context must outlive an
    # elder row removal (it stays referenced for audit), unlike call-scoped data.
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elders.id"), nullable=False
    )
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CallbackRequest(Base):
    __tablename__ = "callback_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elders.id"), nullable=False
    )
    requested_time_text: Mapped[str] = mapped_column(Text, nullable=False)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SmsMessage(Base):
    __tablename__ = "sms_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    elder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("elders.id"), nullable=False
    )
    to_number: Mapped[str] = mapped_column(Text, nullable=False)
    template_key: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    telnyx_message_id: Mapped[str | None] = mapped_column(Text, unique=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
```

(No new imports needed: `BigInteger`, `DateTime`, `ForeignKey`, `Text`, `text`, `func`, `JSONB`, `UUID`, `Mapped`, `mapped_column`, `Any`, `uuid`, `datetime` are all already imported at the top of `models.py`.)

- [ ] Step 4: Run test, verify PASS
  - `cd apps/api && uv run pytest tests/test_phase3_models.py -v`

- [ ] Step 5: Commit
  - `git add apps/api/src/usan_api/db/models.py apps/api/tests/test_phase3_models.py && git commit -m "feat(api): FollowUpFlag, CallbackRequest, SmsMessage ORM models"`

---

### Task B3: Repository — follow_up_flags.py

**Files:**
- Create: `apps/api/src/usan_api/repositories/follow_up_flags.py`
- Test: `apps/api/tests/test_follow_up_flags_repo.py`

> Depends-on: requires Part B migration 0011 + the FollowUpFlag model (B1, B2).

- [ ] Step 1: Write the failing test

```python
# apps/api/tests/test_follow_up_flags_repo.py
"""follow_up_flags repository: create + filtered list (mirrors wellness repo)."""

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories import follow_up_flags as repo


async def _seed_call_and_elder(url: str) -> tuple[uuid.UUID, uuid.UUID]:
    engine = create_async_engine(url, poolclass=NullPool)
    eid, cid = uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO elders (id, name, phone_e164, timezone) "
                    "VALUES (:e, 'Flag Elder', :p, 'UTC')"
                ),
                {"e": str(eid), "p": f"+1555{str(eid.int)[:7]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO calls (id, elder_id, direction, status) "
                    "VALUES (:c, :e, 'outbound', 'completed')"
                ),
                {"c": str(cid), "e": str(eid)},
            )
    finally:
        await engine.dispose()
    return cid, eid


def test_create_and_list_follow_up_flag(async_database_url):
    cid, eid = asyncio.run(_seed_call_and_elder(async_database_url))
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _run():
        async with factory() as db:
            row = await repo.create_follow_up_flag(
                db, call_id=cid, elder_id=eid, severity="urgent",
                category="medical", reason="chest pain reported",
            )
            await db.commit()
            assert isinstance(row.id, int)
            assert row.status == "open"
            assert row.severity == "urgent"

            all_flags = await repo.list_flags(db)
            assert any(f.id == row.id for f in all_flags)

            by_elder = await repo.list_flags(db, elder_id=eid)
            assert [f.id for f in by_elder] == [row.id]

            open_only = await repo.list_flags(db, status="open")
            assert any(f.id == row.id for f in open_only)
            closed = await repo.list_flags(db, status="closed")
            assert all(f.id != row.id for f in closed)

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(engine.dispose())
```

- [ ] Step 2: Run test, verify it FAILS
  - `cd apps/api && uv run pytest tests/test_follow_up_flags_repo.py -v`
  - RED reason: `from usan_api.repositories import follow_up_flags as repo` raises `ModuleNotFoundError` — the module does not exist.

- [ ] Step 3: Implement

```python
# apps/api/src/usan_api/repositories/follow_up_flags.py
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import FollowUpFlag


async def create_follow_up_flag(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    elder_id: uuid.UUID,
    severity: str,
    category: str,
    reason: str | None,
) -> FollowUpFlag:
    row = FollowUpFlag(
        call_id=call_id,
        elder_id=elder_id,
        severity=severity,
        category=category,
        reason=reason,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def list_flags(
    db: AsyncSession,
    *,
    status: str | None = None,
    elder_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[FollowUpFlag]:
    """Most-recent flags, optionally filtered by status/elder (newest first)."""
    stmt = select(FollowUpFlag)
    if status is not None:
        stmt = stmt.where(FollowUpFlag.status == status)
    if elder_id is not None:
        stmt = stmt.where(FollowUpFlag.elder_id == elder_id)
    stmt = stmt.order_by(FollowUpFlag.created_at.desc(), FollowUpFlag.id.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())
```

- [ ] Step 4: Run test, verify PASS
  - `cd apps/api && uv run pytest tests/test_follow_up_flags_repo.py -v`

- [ ] Step 5: Commit
  - `git add apps/api/src/usan_api/repositories/follow_up_flags.py apps/api/tests/test_follow_up_flags_repo.py && git commit -m "feat(api): follow_up_flags repository (create + filtered list)"`

---

### Task B4: Schemas — FlagForFollowupRequest + FollowupFlaggedResponse

**Files:**
- Modify: `apps/api/src/usan_api/schemas/tools.py` (add after `LoggedResponse`, ~line 27; add `Literal` to the `typing` import at line 4)
- Test: `apps/api/tests/test_tool_models.py` (append; the file already validates tool request models)

- [ ] Step 1: Write the failing test (append to `apps/api/tests/test_tool_models.py`)

```python
def test_flag_for_followup_request_valid():
    import uuid

    from usan_api.schemas.tools import FlagForFollowupRequest

    cid = uuid.uuid4()
    req = FlagForFollowupRequest(
        call_id=cid, severity="urgent", category="medical", reason="chest pain"
    )
    assert req.call_id == cid
    assert req.severity == "urgent"
    assert req.category == "medical"


def test_flag_for_followup_rejects_bad_enums():
    import uuid

    import pytest
    from pydantic import ValidationError

    from usan_api.schemas.tools import FlagForFollowupRequest

    with pytest.raises(ValidationError):
        FlagForFollowupRequest(
            call_id=uuid.uuid4(), severity="emergency", category="medical", reason="x"
        )
    with pytest.raises(ValidationError):
        FlagForFollowupRequest(
            call_id=uuid.uuid4(), severity="urgent", category="weather", reason="x"
        )


def test_flag_for_followup_reason_max_length():
    import uuid

    import pytest
    from pydantic import ValidationError

    from usan_api.schemas.tools import FlagForFollowupRequest

    with pytest.raises(ValidationError):
        FlagForFollowupRequest(
            call_id=uuid.uuid4(), severity="routine", category="other", reason="x" * 2001
        )


def test_followup_flagged_response_shape():
    from usan_api.schemas.tools import FollowupFlaggedResponse

    assert FollowupFlaggedResponse(id=7).id == 7
```

- [ ] Step 2: Run test, verify it FAILS
  - `cd apps/api && uv run pytest tests/test_tool_models.py -k followup -v`
  - RED reason: `from usan_api.schemas.tools import FlagForFollowupRequest` / `FollowupFlaggedResponse` raises `ImportError` — neither model is defined.

- [ ] Step 3: Implement
  - Change the `typing` import at line 4 of `schemas/tools.py` from `from typing import Any` to:

```python
from typing import Any, Literal
```

  - Add after the `LoggedResponse` class (line 25-26):

```python
class FlagForFollowupRequest(ToolCallRequest):
    severity: Literal["routine", "urgent"]
    category: Literal["medical", "emotional", "medication", "safety", "other"]
    reason: str = Field(max_length=2000)


class FollowupFlaggedResponse(BaseModel):
    id: int
```

- [ ] Step 4: Run test, verify PASS
  - `cd apps/api && uv run pytest tests/test_tool_models.py -k followup -v`

- [ ] Step 5: Commit
  - `git add apps/api/src/usan_api/schemas/tools.py apps/api/tests/test_tool_models.py && git commit -m "feat(api): FlagForFollowupRequest + FollowupFlaggedResponse schemas"`

---

### Task B5: Endpoint POST /v1/tools/flag_for_followup + FOLLOWUP_FLAGS_TOTAL metric

**Files:**
- Modify: `apps/api/src/usan_api/observability/custom_metrics.py` (add `FOLLOWUP_FLAGS_TOTAL` after `TOOL_CALLS_TOTAL`, ~line 43)
- Modify: `apps/api/src/usan_api/routers/tools.py` (add endpoint after `log_wellness`, ~line 76; extend imports)
- Test: `apps/api/tests/test_tools.py` (append)

> Depends-on: requires Part B migration 0011 + models + repo (B1-B3).

- [ ] Step 1: Write the failing test (append to `apps/api/tests/test_tools.py`)

```python
def test_flag_for_followup_ok(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={
            "call_id": call_id,
            "severity": "urgent",
            "category": "medical",
            "reason": "reported chest pain",
        },
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert isinstance(r.json()["id"], int)


def test_flag_for_followup_requires_token(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={"call_id": call_id, "severity": "routine", "category": "other", "reason": "x"},
    )
    assert r.status_code == 401


def test_flag_for_followup_call_id_mismatch_403(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    wrong = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={"call_id": call_id, "severity": "routine", "category": "other", "reason": "x"},
        headers=_auth(wrong),
    )
    assert r.status_code == 403


def test_flag_for_followup_unknown_call_404(client, mock_dispatch):
    cid = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={"call_id": cid, "severity": "routine", "category": "other", "reason": "x"},
        headers=_auth(cid),
    )
    assert r.status_code == 404


def test_flag_for_followup_bad_enum_422(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={"call_id": call_id, "severity": "panic", "category": "medical", "reason": "x"},
        headers=_auth(call_id),
    )
    assert r.status_code == 422


def test_flag_for_followup_increments_metric(client, mock_dispatch):
    from usan_api.observability.custom_metrics import FOLLOWUP_FLAGS_TOTAL

    before = FOLLOWUP_FLAGS_TOTAL.labels(severity="urgent", category="safety")._value.get()
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={"call_id": call_id, "severity": "urgent", "category": "safety", "reason": "fell"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    after = FOLLOWUP_FLAGS_TOTAL.labels(severity="urgent", category="safety")._value.get()
    assert after == before + 1
```

- [ ] Step 2: Run test, verify it FAILS
  - `cd apps/api && uv run pytest tests/test_tools.py -k flag_for_followup -v`
  - RED reason: `POST /v1/tools/flag_for_followup` is not registered → FastAPI returns 404 for the OK/enum tests (so `assert r.status_code == 200`/`422` fail), and `from usan_api.observability.custom_metrics import FOLLOWUP_FLAGS_TOTAL` raises `ImportError`.

- [ ] Step 3: Implement

  - In `custom_metrics.py`, add after the `TOOL_CALLS_TOTAL` block (line 39-43):

```python
# severity: routine|urgent ; category: the bounded FlagForFollowupRequest enum
# (medical, emotional, medication, safety, other). NEVER the free-text reason (PHI).
FOLLOWUP_FLAGS_TOTAL = Counter(
    "usan_followup_flags",
    "Follow-up flags created.",
    labelnames=("severity", "category"),
)
```

  - In `routers/tools.py`, extend the `custom_metrics` import (line 14) and add the `follow_up_flags` repo + schema imports. Change line 14 from `from usan_api.observability.custom_metrics import CALLS_TOTAL, track_tool` to:

```python
from usan_api.observability.custom_metrics import (
    CALLS_TOTAL,
    FOLLOWUP_FLAGS_TOTAL,
    track_tool,
)
```

  - Add to the repository imports (after line 16 `from usan_api.repositories import calls as calls_repo`):

```python
from usan_api.repositories import follow_up_flags as follow_up_flags_repo
```

  - Add to the `schemas.tools` import block (lines 21-34), inserting alphabetically/where convenient:

```python
    FlagForFollowupRequest,
    FollowupFlaggedResponse,
```

  - Add the endpoint after `log_wellness` (after line 76):

```python
@router.post("/flag_for_followup", response_model=FollowupFlaggedResponse)
@track_tool("flag_for_followup")
async def flag_for_followup(
    body: FlagForFollowupRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> FollowupFlaggedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    elder_id = _require_elder(call)
    row = await follow_up_flags_repo.create_follow_up_flag(
        db,
        call_id=call.id,
        elder_id=elder_id,
        severity=body.severity,
        category=body.category,
        reason=body.reason,
    )
    await db.commit()
    # Increment AFTER commit so a crash can't double-count. Labels are bounded
    # enums only — never body.reason (free-text PHI).
    FOLLOWUP_FLAGS_TOTAL.labels(severity=body.severity, category=body.category).inc()
    # Don't log body.reason: it can carry clinical content; it's persisted to the DB.
    logger.bind(call_id=str(call.id)).info("Flagged for follow-up")
    return FollowupFlaggedResponse(id=row.id)
```

- [ ] Step 4: Run test, verify PASS
  - `cd apps/api && uv run pytest tests/test_tools.py -k flag_for_followup -v`

- [ ] Step 5: Commit
  - `git add apps/api/src/usan_api/observability/custom_metrics.py apps/api/src/usan_api/routers/tools.py apps/api/tests/test_tools.py && git commit -m "feat(api): POST /v1/tools/flag_for_followup + usan_followup_flags metric"`

---

### Task B6: Schema — FollowupFlagSummary (CREATE schemas/admin_tools.py)

**Files:**
- Create: `apps/api/src/usan_api/schemas/admin_tools.py`
- Test: `apps/api/tests/test_admin_tools_schemas.py`

> R7: this file is CREATED in Part B; Parts C and D ADD their summary models to it (additive).

- [ ] Step 1: Write the failing test

```python
# apps/api/tests/test_admin_tools_schemas.py
"""Admin read-model summaries for the Phase-3 tool tables."""

import uuid
from datetime import UTC, datetime


def test_followup_flag_summary_from_attributes():
    from usan_api.schemas.admin_tools import FollowupFlagSummary

    class _Row:
        id = 5
        call_id = uuid.uuid4()
        elder_id = uuid.uuid4()
        severity = "urgent"
        category = "medical"
        reason = "chest pain"
        status = "open"
        created_at = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)

    s = FollowupFlagSummary.model_validate(_Row())
    assert s.id == 5
    assert s.severity == "urgent"
    assert s.reason == "chest pain"  # admin read exposes PHI reason (audited)
    assert s.status == "open"
```

- [ ] Step 2: Run test, verify it FAILS
  - `cd apps/api && uv run pytest tests/test_admin_tools_schemas.py -v`
  - RED reason: `from usan_api.schemas.admin_tools import FollowupFlagSummary` raises `ModuleNotFoundError` — file does not exist.

- [ ] Step 3: Implement

```python
# apps/api/src/usan_api/schemas/admin_tools.py
"""Admin read-model summaries for the Phase-3 tool tables (design §5/§6).

`from_attributes=True` lets these validate directly from the ORM rows the
repositories return. FollowupFlagSummary intentionally exposes `reason` (PHI):
the admin endpoint is session-gated and audited (see routers/admin_tools.py).
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class FollowupFlagSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    call_id: uuid.UUID
    elder_id: uuid.UUID
    severity: str
    category: str
    reason: str | None
    status: str
    created_at: datetime
```

- [ ] Step 4: Run test, verify PASS
  - `cd apps/api && uv run pytest tests/test_admin_tools_schemas.py -v`

- [ ] Step 5: Commit
  - `git add apps/api/src/usan_api/schemas/admin_tools.py apps/api/tests/test_admin_tools_schemas.py && git commit -m "feat(api): FollowupFlagSummary admin schema (create schemas/admin_tools.py)"`

---

### Task B7: Admin endpoint GET /v1/admin/follow-up-flags (CREATE routers/admin_tools.py) + register in main.py + audit

**Files:**
- Create: `apps/api/src/usan_api/routers/admin_tools.py`
- Modify: `apps/api/src/usan_api/main.py` (import + `include_router`, next to `admin_variable_catalog`)
- Test: `apps/api/tests/test_admin_tools_api.py`

> R7: created + registered in main.py ONCE here; C and D add their routes to the existing file (no re-register).
> Depends-on: requires Part B migration 0011 + models + repo (B1-B3).

- [ ] Step 1: Write the failing test

```python
# apps/api/tests/test_admin_tools_api.py
"""Admin read endpoint GET /v1/admin/follow-up-flags (session-gated + audited).

Uses the cookie-jar admin_session fixture exactly like test_admin_elders_api /
test_variable_catalog_api (the cookie is set on the shared `client`).
"""

import time
import uuid

import jwt


def _service_token(call_id: str, secret: str = "s" * 32) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )


_OP = {"Authorization": "Bearer " + "o" * 32}


def _create_elder(client) -> str:
    r = client.post(
        "/v1/elders",
        json={
            "name": "Ada",
            "phone_e164": f"+1555{str(uuid.uuid4().int)[:7]}",
            "timezone": "UTC",
            "metadata": {},
        },
        headers=_OP,
    )
    assert r.status_code == 201
    return r.json()["id"]


def _enqueue(client, elder_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": f"flag-{uuid.uuid4()}", "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 202
    return r.json()["id"]


def _seed_flag(client, *, severity="urgent", category="medical", reason="reported chest pain"):
    from usan_api import livekit_dispatch
    from unittest.mock import AsyncMock

    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={"call_id": call_id, "severity": severity, "category": category, "reason": reason},
        headers={"Authorization": f"Bearer {_service_token(call_id)}"},
    )
    assert r.status_code == 200, r.text
    return elder_id, call_id, r.json()["id"]


def test_follow_up_flags_requires_session(client, mock_dispatch):
    assert client.get("/v1/admin/follow-up-flags").status_code == 401


def test_follow_up_flags_list_and_filter(client, mock_dispatch, admin_session):
    elder_id, _call_id, flag_id = _seed_flag(client)

    rows = client.get("/v1/admin/follow-up-flags").json()
    me = next(f for f in rows if f["id"] == flag_id)
    assert me["severity"] == "urgent"
    assert me["category"] == "medical"
    assert me["reason"] == "reported chest pain"
    assert me["status"] == "open"

    by_elder = client.get(f"/v1/admin/follow-up-flags?elder_id={elder_id}").json()
    assert [f["id"] for f in by_elder] == [flag_id]

    open_rows = client.get("/v1/admin/follow-up-flags?status=open").json()
    assert any(f["id"] == flag_id for f in open_rows)
    closed_rows = client.get("/v1/admin/follow-up-flags?status=closed").json()
    assert all(f["id"] != flag_id for f in closed_rows)

    # Over-cap limit rejected by Query(le=...).
    assert client.get("/v1/admin/follow-up-flags?limit=100000").status_code == 422


def test_follow_up_flags_read_is_audited_phi_free(client, mock_dispatch, admin_session):
    # F7: AuditEntryOut serializes `detail`. The admin read of PHI rows must be
    # audited, but the audit entry itself must carry NO PHI (no reason text).
    _elder_id, _call_id, _flag_id = _seed_flag(client, reason="secret chest pain note")
    client.get("/v1/admin/follow-up-flags")
    rows = client.get("/v1/admin/audit?action=follow_up_flags.list").json()
    assert rows, "follow-up-flags read must write an audit entry"
    entry = rows[0]
    blob = (str(entry["detail"]) + str(entry["entity_type"]) + str(entry["entity_id"])).lower()
    assert "secret" not in blob
    assert "chest pain" not in blob
```

(Note: `mock_dispatch` is a fixture in `test_tools.py`, not shared via conftest — duplicate the tiny `mock_dispatch` fixture into this file, OR move it to conftest. To keep this file self-contained, prepend the fixture:)

```python
import pytest


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer, livekit_dispatch

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)
```

- [ ] Step 2: Run test, verify it FAILS
  - `cd apps/api && uv run pytest tests/test_admin_tools_api.py -v`
  - RED reason: `routers/admin_tools.py` does not exist and is not registered, so `GET /v1/admin/follow-up-flags` returns 404 (the list/filter test asserts on JSON shape and fails), and no audit row is written.

- [ ] Step 3: Implement

  - Create `apps/api/src/usan_api/routers/admin_tools.py`:

```python
# apps/api/src/usan_api/routers/admin_tools.py
"""Admin read endpoints for the Phase-3 tool tables (design §5/§6).

Session-gated (require_admin_session). Reads that expose PHI (`follow_up_flags`)
record a PHI-FREE audit entry: only the actor + filter shape, never `reason`.
C and D ADD their summary route to this file (additive; no re-register).
"""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.admin_actor import get_actor_email
from usan_api.auth import require_admin_session
from usan_api.db.session import get_db
from usan_api.repositories import admin_audit
from usan_api.repositories import follow_up_flags as follow_up_flags_repo
from usan_api.schemas.admin_tools import FollowupFlagSummary

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin-tools"],
    dependencies=[Depends(require_admin_session)],
)


@router.get("/follow-up-flags", response_model=list[FollowupFlagSummary])
async def list_follow_up_flags(
    status: str | None = Query(default=None, max_length=32),
    elder_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    actor: str = Depends(get_actor_email),
) -> list[FollowupFlagSummary]:
    rows = await follow_up_flags_repo.list_flags(
        db, status=status, elder_id=elder_id, limit=limit
    )
    # PHI read (reason) -> audit. Detail carries only the filter shape + count,
    # NEVER the reason text or an elder's name/phone (PHI-free; spec §9).
    await admin_audit.record(
        db,
        actor_email=actor,
        action="follow_up_flags.list",
        entity_type="follow_up_flag",
        entity_id=str(elder_id) if elder_id is not None else None,
        detail={"status": status, "count": len(rows)},
    )
    await db.commit()
    return [FollowupFlagSummary.model_validate(r) for r in rows]
```

  - In `main.py`, add `admin_tools` to the routers import block (lines 15-28, next to `admin_variable_catalog`):

```python
from usan_api.routers import (
    admin_audit,
    admin_elders,
    admin_profiles,
    admin_tools,
    admin_users,
    admin_variable_catalog,
    auth,
    calls,
    dnc,
    elders,
    runtime,
    tools,
    webhooks,
)
```

  - And register it next to `admin_variable_catalog` (after line 122 `app.include_router(admin_variable_catalog.router)`):

```python
    app.include_router(admin_tools.router)
```

- [ ] Step 4: Run test, verify PASS
  - `cd apps/api && uv run pytest tests/test_admin_tools_api.py -v`

- [ ] Step 5: Commit
  - `git add apps/api/src/usan_api/routers/admin_tools.py apps/api/src/usan_api/main.py apps/api/tests/test_admin_tools_api.py && git commit -m "feat(api): GET /v1/admin/follow-up-flags (audited) + register admin_tools router"`

---

### Task B8: Agent — @function_tool flag_for_followup + _do_ helper + _TOOL_REGISTRY insert + api_client

**Files:**
- Modify: `services/agent/src/usan_agent/api_client.py` (add `flag_for_followup` after `log_wellness`, ~line 80)
- Modify: `services/agent/src/usan_agent/check_in.py` (add `_do_flag_for_followup`, the `@function_tool`, and ONE `_TOOL_REGISTRY` line per R5)
- Test: `services/agent/tests/test_check_in.py` (append) + `services/agent/tests/test_api_client.py` (append, if it exists; else add to test_check_in)

> R5: insert ONLY `"flag_for_followup": flag_for_followup,` immediately BEFORE the `"end_call": end_call,` line.
> R1-R4: do NOT touch `TOOL_NAMES`, `_select_tools`, `ToolsConfig` default, or the four-tool tests — Part A owns those.

- [ ] Step 1: Write the failing test (append to `services/agent/tests/test_check_in.py`)

```python
async def test_do_flag_for_followup_calls_api_and_acks(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(check_in.api_client, "flag_for_followup", spy)
    result = await check_in._do_flag_for_followup(
        _data(), severity="urgent", category="medical", reason="chest pain"
    )
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs == {"severity": "urgent", "category": "medical", "reason": "chest pain"}
    assert spy.await_args.args[0] == "call-1"
    assert isinstance(result, str)
    assert result  # a calm spoken confirmation


async def test_do_flag_for_followup_handles_api_failure(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(check_in.api_client, "flag_for_followup", _boom)
    result = await check_in._do_flag_for_followup(
        _data(), severity="routine", category="other", reason="x"
    )
    assert isinstance(result, str)
    assert result  # graceful fallback, no exception


def test_flag_for_followup_in_tool_registry():
    # R5: registry grows additively; flag_for_followup is registered before end_call.
    assert "flag_for_followup" in check_in._TOOL_REGISTRY
    keys = list(check_in._TOOL_REGISTRY)
    assert keys.index("flag_for_followup") < keys.index("end_call")
```

  And add an api_client payload test (append to `services/agent/tests/test_api_client.py` — confirm the file exists; if not, place this in `test_check_in.py`):

```python
async def test_api_client_flag_for_followup_payload(monkeypatch):
    import usan_agent.api_client as api_client

    captured = {}

    async def _post_tool(tool, call_id, settings, payload):
        captured["tool"] = tool
        captured["payload"] = payload
        return {"id": 1}

    monkeypatch.setattr(api_client, "_post_tool", _post_tool)
    await api_client.flag_for_followup(
        "call-9", _settings(), severity="urgent", category="safety", reason="fell"
    )
    assert captured["tool"] == "flag_for_followup"
    assert captured["payload"] == {
        "severity": "urgent",
        "category": "safety",
        "reason": "fell",
    }
```

(`_settings()` helper exists in `test_check_in.py`; if placing the api_client test there it is in scope. If `test_api_client.py` exists, copy the minimal `_settings()` builder from `test_check_in.py` into it.)

- [ ] Step 2: Run test, verify it FAILS
  - `cd services/agent && uv run pytest tests/test_check_in.py -k flag_for_followup -v`
  - RED reason: `check_in._do_flag_for_followup` and `api_client.flag_for_followup` are not defined (`AttributeError`), and `"flag_for_followup"` is absent from `_TOOL_REGISTRY`.

- [ ] Step 3: Implement

  - In `services/agent/src/usan_agent/api_client.py`, add after `log_wellness` (after line 79):

```python
async def flag_for_followup(
    call_id: str,
    settings: Settings,
    *,
    severity: str,
    category: str,
    reason: str,
) -> None:
    await _post_tool(
        "flag_for_followup",
        call_id,
        settings,
        {"severity": severity, "category": category, "reason": reason},
    )
```

  - In `services/agent/src/usan_agent/check_in.py`, add the `_do_*` helper after `_do_log_wellness` (after line 67):

```python
async def _do_flag_for_followup(
    data: CheckInData, *, severity: str, category: str, reason: str
) -> str:
    try:
        await api_client.flag_for_followup(
            data.call_id, data.settings, severity=severity, category=category, reason=reason
        )
    except Exception:
        logger.bind(call_id=data.call_id).warning("flag_for_followup tool failed")
        return "I've made a note of that, and I'll make sure someone follows up."
    return "Thank you. I've flagged this so someone can follow up with you."
```

  - Add the `@function_tool` (after the `get_today_meds` tool, before `end_call`, ~line 181):

```python
@function_tool
async def flag_for_followup(
    ctx: RunContext[CheckInData],
    severity: str,
    category: str,
    reason: str,
) -> str:
    """Flag this call for a human to follow up on.

    Args:
        severity: "routine" for a non-urgent note, "urgent" for prompt attention.
        category: One of "medical", "emotional", "medication", "safety", "other".
        reason: A short description of what should be followed up on.
    """
    return await _do_flag_for_followup(
        ctx.userdata, severity=severity, category=category, reason=reason
    )
```

  - Insert ONE line into `_TOOL_REGISTRY` immediately BEFORE the `"end_call": end_call,` line (R5). The dict becomes:

```python
_TOOL_REGISTRY: dict[str, Any] = {
    "log_wellness": log_wellness,
    "log_medication": log_medication,
    "get_today_meds": get_today_meds,
    "flag_for_followup": flag_for_followup,
    "end_call": end_call,
}
```

- [ ] Step 4: Run test, verify PASS
  - `cd services/agent && uv run pytest tests/test_check_in.py -k flag_for_followup -v` and `cd services/agent && uv run pytest -q` (the Part-A registry-driven four-tool tests stay green: `expected` recomputes from `_TOOL_REGISTRY` minus `send_sms`, and `flag_for_followup` is in `DEFAULT_AGENT_CONFIG.tools.enabled` after Part A).

- [ ] Step 5: Commit
  - `git add services/agent/src/usan_agent/check_in.py services/agent/src/usan_agent/api_client.py services/agent/tests/test_check_in.py services/agent/tests/test_api_client.py && git commit -m "feat(agent): flag_for_followup function tool + api_client + registry entry"`

---

### Task B9: Grafana-as-code — usan_followup_flags panel + documented urgent alert rule

**Files:**
- Modify: `infra/grafana/dashboards/system.json` (append panel id 11)
- Modify: `apps/api/src/usan_api/observability/custom_metrics.py` (docstring/comment for the alert expr) OR add the alert expr as a comment in the spec — see note
- Test: `scripts/tests/test_system_dashboard.py` (append assertions for the new panel)

> R9c / F3: append to `infra/grafana/dashboards/system.json`. **system.json panels allocated B=id11/y29, D=id12/y37.** Validate via the EXISTING `scripts/tests` dashboard contract (stdlib json only — no PyYAML). Alert rules are NOT codified in `infra/grafana` (no alerting JSON exists), so per F3 ship the panel and DOCUMENT the PromQL alert expr; the notification channel is a deploy step (spec §5.1).

- [ ] Step 1: Write the failing test (append to `scripts/tests/test_system_dashboard.py`)

```python
def test_system_has_followup_flags_panel():
    # Phase-3 B9 (F3): id 11 at y=29, the urgent-flags series.
    doc = load_dashboard("system.json")
    panel = next((p for p in iter_panels(doc) if p.get("id") == 11), None)
    assert panel is not None, "expected follow-up-flags panel id 11"
    assert panel["gridPos"] == {"x": 0, "y": 29, "w": 24, "h": 8}
    exprs = " ".join(t.get("expr", "") for t in panel.get("targets", []))
    assert "usan_followup_flags_total" in exprs
    # The panel must surface the urgent series (alert rule fires on severity="urgent").
    assert 'severity="urgent"' in exprs


def test_system_dashboard_still_valid_with_followup_panel():
    doc = load_dashboard("system.json")
    assert validate_dashboard(doc) == []
    assert gridpos_overlaps(list(iter_panels(doc))) == []
```

- [ ] Step 2: Run test, verify it FAILS
  - `cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine && python -m pytest scripts/tests/test_system_dashboard.py -k followup -v`
  - RED reason: no panel with `id == 11` exists in `system.json` (`panel is None` → assertion fails).

- [ ] Step 3: Implement
  - Append a new panel object to the `panels` array of `infra/grafana/dashboards/system.json`, mirroring the existing prometheus timeseries panel format (panel id 5). Insert as the last element of `panels` (after panel id 10):

```json
    {
      "id": 11,
      "type": "timeseries",
      "title": "Follow-up flags (severity / category)",
      "gridPos": { "h": 8, "w": 24, "x": 0, "y": 29 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        {
          "refId": "A",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "sum by (severity, category) (rate(usan_followup_flags_total[$__rate_interval]))",
          "legendFormat": "{{severity}} · {{category}}",
          "range": true
        },
        {
          "refId": "B",
          "datasource": { "type": "prometheus", "uid": "prometheus" },
          "expr": "sum (increase(usan_followup_flags_total{severity=\"urgent\"}[1h]))",
          "legendFormat": "urgent (1h)",
          "range": true
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "short",
          "custom": {
            "drawStyle": "line",
            "lineWidth": 1,
            "fillOpacity": 10,
            "showPoints": "never"
          }
        },
        "overrides": []
      },
      "options": {
        "legend": { "displayMode": "list", "placement": "bottom", "showLegend": true },
        "tooltip": { "mode": "multi", "sort": "desc" }
      }
    }
```

  - Document the alert rule. Since `infra/grafana` has no codified alert-rules tree, add the PromQL alert expression as a comment next to the metric declaration in `custom_metrics.py` (the single source the alert references), extending the comment added in B5:

```python
# severity: routine|urgent ; category: the bounded FlagForFollowupRequest enum
# (medical, emotional, medication, safety, other). NEVER the free-text reason (PHI).
#
# Grafana alert (deploy step — notification channel supplied by the operator, spec §5.1):
#   ALERT  usan_urgent_followup_flag
#   EXPR   sum(increase(usan_followup_flags_total{severity="urgent"}[10m])) > 0
#   FOR    0m
#   on the `prometheus` datasource; routed to the operator's email/Slack contact point.
FOLLOWUP_FLAGS_TOTAL = Counter(
    "usan_followup_flags",
    "Follow-up flags created.",
    labelnames=("severity", "category"),
)
```

- [ ] Step 4: Run test, verify PASS
  - `cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine && python -m pytest scripts/tests/test_system_dashboard.py scripts/tests/test_dashboards_collection.py -v` (panel valid, no overlap — y=29 sits exactly below panel id 10 whose y+h=29)

- [ ] Step 5: Commit
  - `git add infra/grafana/dashboards/system.json apps/api/src/usan_api/observability/custom_metrics.py scripts/tests/test_system_dashboard.py && git commit -m "feat(infra): system.json follow-up-flags panel + documented urgent alert rule"`

---

**Part B notes for the executor:**
- B executes strictly after Part A. By the time B runs, Part A has already: deleted the line-20 frozenset in `agent_config.py` schema and imported `TOOL_NAMES` from `tool_catalog`; set both `DEFAULT_AGENT_CONFIG.tools.enabled` defaults to the final 7-list; rewritten `_select_tools(tools: "ToolsConfig")` (R3); and made the four-tool agent tests registry-driven (R4). B is purely additive over that baseline and touches none of those symbols.
- F3 cross-part coordination: B claims **system.json panel id 11 at gridPos {x:0,y:29,w:24,h:8}**; Part D claims id 12 at y:37. Stated in the B9 task so D does not collide.
- F7: confirmed `AuditEntryOut` serializes `detail` (read `routers/admin_audit.py` + `schemas/admin.py`), so B7's audit test asserts PHI-free by inspecting the serialized `detail`/`entity_*` fields of the `follow_up_flags.list` audit entry.
- Real-file confirmations baked in: agent tool introspection uses `t.id` (not `.name`) per the existing `test_check_in.py`; `track_tool` + `_authorize_call` + `_require_elder` + `await db.commit()` pattern mirrored from `log_wellness`; metric incremented AFTER commit; `mock_dispatch` fixture is local to `test_tools.py` (duplicated into the new admin test file); `models.py` already imports every symbol the 3 new models need.

---

## Part C — `schedule_callback` (additive only)

> **Depends-on (R10):** Every DB-table task below **requires Part A** (catalog/`TOOL_NAMES`, `_select_tools`, the 7-tool `DEFAULT_AGENT_CONFIG.tools.enabled`, registry-driven four-tool tests) **and Part B** (migration `0011` + `db/models.py` models `FollowUpFlag`/`CallbackRequest`/`SmsMessage`, plus `routers/admin_tools.py`/`schemas/admin_tools.py` created & registered in `main.py`). Execute strictly A→B→**C**→D.
>
> **Integration-rule compliance:** Part C is **ADDITIVE ONLY**. It does **not** touch `TOOL_NAMES` (R1), `ToolsConfig` default `enabled` (R2), `_select_tools` (R3), the four-tool agent tests (R4), or re-create/re-register `admin_tools` (R7). It inserts exactly one `_TOOL_REGISTRY` line (R5), adds only `schedule_callback`'s three agent pieces (R6), and adds one summary model + one route to the existing `admin_tools` files (R7).

---

### Task C1: `repositories/callback_requests.py`

**Files:**
- Create: `apps/api/src/usan_api/repositories/callback_requests.py`
- Test: `apps/api/tests/test_callback_requests_repo.py`

Mirrors `repositories/wellness.py` async `add/flush/refresh` + `select`. **Requires Part B migration 0011 + `CallbackRequest` model** (R10).

- [ ] **Step 1: Write the failing test** → `apps/api/tests/test_callback_requests_repo.py`

```python
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories import callback_requests as cb_repo


async def _seed_call_and_elder(db) -> tuple[uuid.UUID, uuid.UUID]:
    elder_id = uuid.uuid4()
    call_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO elders (id, name, phone_e164, timezone) "
            "VALUES (CAST(:id AS uuid), 'Ada', :p, 'UTC')"
        ),
        {"id": str(elder_id), "p": f"+1555{str(elder_id.int)[:7]}"},
    )
    await db.execute(
        text(
            "INSERT INTO calls (id, elder_id, direction, status) "
            "VALUES (CAST(:cid AS uuid), CAST(:eid AS uuid), 'outbound', 'in_progress')"
        ),
        {"cid": str(call_id), "eid": str(elder_id)},
    )
    return call_id, elder_id


@pytest.fixture
async def db(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


async def test_create_callback_request_persists_row(db):
    call_id, elder_id = await _seed_call_and_elder(db)
    when = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
    row = await cb_repo.create_callback_request(
        db,
        call_id=call_id,
        elder_id=elder_id,
        requested_time_text="tomorrow afternoon",
        requested_at=when,
        notes="prefers a call back",
    )
    await db.commit()
    assert isinstance(row.id, int)
    assert row.call_id == call_id
    assert row.elder_id == elder_id
    assert row.requested_time_text == "tomorrow afternoon"
    assert row.requested_at == when
    assert row.notes == "prefers a call back"
    assert row.status == "open"


async def test_create_callback_request_allows_null_requested_at(db):
    call_id, elder_id = await _seed_call_and_elder(db)
    row = await cb_repo.create_callback_request(
        db,
        call_id=call_id,
        elder_id=elder_id,
        requested_time_text="sometime soon",
        requested_at=None,
        notes=None,
    )
    await db.commit()
    assert row.requested_at is None
    assert row.notes is None


async def test_list_callback_requests_filters_by_status(db):
    call_id, elder_id = await _seed_call_and_elder(db)
    await cb_repo.create_callback_request(
        db,
        call_id=call_id,
        elder_id=elder_id,
        requested_time_text="first",
        requested_at=None,
        notes=None,
    )
    await db.commit()
    rows = await cb_repo.list_callback_requests(db, status="open", limit=50)
    assert any(r.requested_time_text == "first" for r in rows)
    none_rows = await cb_repo.list_callback_requests(db, status="resolved", limit=50)
    assert all(r.status == "resolved" for r in none_rows)
```

- [ ] **Step 2: Run test, verify it FAILS**
  `cd apps/api && uv run pytest tests/test_callback_requests_repo.py -v`
  **Expected RED:** `ImportError` / `ModuleNotFoundError: usan_api.repositories.callback_requests` (the module does not exist yet).

- [ ] **Step 3: Implement** → `apps/api/src/usan_api/repositories/callback_requests.py`

```python
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import CallbackRequest


async def create_callback_request(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    elder_id: uuid.UUID,
    requested_time_text: str,
    requested_at: datetime | None,
    notes: str | None,
) -> CallbackRequest:
    row = CallbackRequest(
        call_id=call_id,
        elder_id=elder_id,
        requested_time_text=requested_time_text,
        requested_at=requested_at,
        notes=notes,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def list_callback_requests(
    db: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[CallbackRequest]:
    """Most-recent callback requests, optionally filtered by status. Bounded by limit."""
    stmt = select(CallbackRequest)
    if status is not None:
        stmt = stmt.where(CallbackRequest.status == status)
    stmt = stmt.order_by(CallbackRequest.created_at.desc(), CallbackRequest.id.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())
```

- [ ] **Step 4: Run test, verify PASS**
  `cd apps/api && uv run pytest tests/test_callback_requests_repo.py -v && ruff check src/usan_api/repositories/callback_requests.py tests/test_callback_requests_repo.py && ruff format src/usan_api/repositories/callback_requests.py tests/test_callback_requests_repo.py && uv run mypy src/usan_api/repositories/callback_requests.py`

- [ ] **Step 5: Commit**
  `git add apps/api/src/usan_api/repositories/callback_requests.py apps/api/tests/test_callback_requests_repo.py && git commit -m "feat(api): callback_requests repository (Phase 3 Part C)"`

---

### Task C2: `schemas/tools.py` — `ScheduleCallbackRequest` + `CallbackScheduledResponse`

**Files:**
- Modify: `apps/api/src/usan_api/schemas/tools.py` (add `from datetime import datetime` is already imported on line 2; append the two models after `CallEndedResponse` block, before `TranscriptSegmentIn` at line 57)
- Test: `apps/api/tests/test_tools_schemas.py`

- [ ] **Step 1: Write the failing test** → `apps/api/tests/test_tools_schemas.py`

```python
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from usan_api.schemas.tools import CallbackScheduledResponse, ScheduleCallbackRequest

_CID = uuid.uuid4()


def test_schedule_callback_request_minimal():
    req = ScheduleCallbackRequest(call_id=_CID, requested_time_text="tomorrow at 3")
    assert req.requested_time_text == "tomorrow at 3"
    assert req.requested_at is None
    assert req.notes is None


def test_schedule_callback_request_full():
    when = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
    req = ScheduleCallbackRequest(
        call_id=_CID,
        requested_time_text="tomorrow at 3",
        requested_at=when,
        notes="prefers afternoons",
    )
    assert req.requested_at == when
    assert req.notes == "prefers afternoons"


def test_schedule_callback_request_rejects_empty_time_text():
    with pytest.raises(ValidationError):
        ScheduleCallbackRequest(call_id=_CID, requested_time_text="")


def test_schedule_callback_request_caps_time_text_length():
    with pytest.raises(ValidationError):
        ScheduleCallbackRequest(call_id=_CID, requested_time_text="x" * 201)


def test_schedule_callback_request_caps_notes_length():
    with pytest.raises(ValidationError):
        ScheduleCallbackRequest(
            call_id=_CID, requested_time_text="soon", notes="y" * 2001
        )


def test_callback_scheduled_response_shape():
    resp = CallbackScheduledResponse(id=42)
    assert resp.id == 42
```

- [ ] **Step 2: Run test, verify it FAILS**
  `cd apps/api && uv run pytest tests/test_tools_schemas.py -v`
  **Expected RED:** `ImportError: cannot import name 'ScheduleCallbackRequest' from 'usan_api.schemas.tools'` (models not yet defined).

- [ ] **Step 3: Implement** → in `apps/api/src/usan_api/schemas/tools.py`, insert immediately after the `CallEndedResponse` class (line 54, before the blank line preceding `class TranscriptSegmentIn` at line 57). `datetime` is already imported (line 2):

```python
class ScheduleCallbackRequest(ToolCallRequest):
    requested_time_text: str = Field(min_length=1, max_length=200)
    requested_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)


class CallbackScheduledResponse(BaseModel):
    id: int
```

- [ ] **Step 4: Run test, verify PASS**
  `cd apps/api && uv run pytest tests/test_tools_schemas.py -v && ruff check src/usan_api/schemas/tools.py tests/test_tools_schemas.py && ruff format src/usan_api/schemas/tools.py tests/test_tools_schemas.py && uv run mypy src/usan_api/schemas/tools.py`

- [ ] **Step 5: Commit**
  `git add apps/api/src/usan_api/schemas/tools.py apps/api/tests/test_tools_schemas.py && git commit -m "feat(api): ScheduleCallbackRequest/CallbackScheduledResponse schemas (Phase 3 Part C)"`

---

### Task C3: `routers/tools.py` — `POST /v1/tools/schedule_callback` + `CALLBACK_REQUESTS_TOTAL`

**Files:**
- Modify: `apps/api/src/usan_api/observability/custom_metrics.py` (add Counter after `TOOL_CALLS_TOTAL`, line 43)
- Modify: `apps/api/src/usan_api/routers/tools.py` (add import of `callback_requests` repo near line 15-20; add `ScheduleCallbackRequest`/`CallbackScheduledResponse` + `CALLBACK_REQUESTS_TOTAL` imports; add endpoint after `log_medication`, before `get_today_meds` at line 100)
- Test: `apps/api/tests/test_schedule_callback_endpoint.py`

Mirrors `log_wellness` EXACTLY: `@router.post` + `@track_tool` + `Depends(require_service_token)` + `Depends(get_db)` + `_authorize_call` + `_require_elder` + repo create + `await db.commit()`; metric incremented **AFTER** commit (spec §7). **Requires Part B migration 0011 + `CallbackRequest` model** (R10). `requested_at` is parsed by Pydantic from an ISO string into `datetime | None`; the raw spoken phrasing is stored in `requested_time_text` — **no NLP**.

- [ ] **Step 1: Write the failing test** → `apps/api/tests/test_schedule_callback_endpoint.py`

```python
import time
import uuid

import jwt
import pytest

from usan_api import livekit_dispatch

_OP = {"Authorization": "Bearer " + "o" * 32}


@pytest.fixture
def mock_dispatch(monkeypatch):
    from unittest.mock import AsyncMock

    from usan_api import dialer

    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", AsyncMock())
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)


def _service_token(call_id: str, secret: str = "s" * 32) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + 300},
        secret,
        algorithm="HS256",
    )


def _auth(call_id: str) -> dict:
    return {"Authorization": f"Bearer {_service_token(call_id)}"}


def _create_elder(client) -> str:
    r = client.post(
        "/v1/elders",
        json={
            "name": "Ada",
            "phone_e164": f"+1555{str(uuid.uuid4().int)[:7]}",
            "timezone": "UTC",
            "metadata": {},
        },
        headers=_OP,
    )
    assert r.status_code == 201
    return r.json()["id"]


def _enqueue(client, elder_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={"elder_id": elder_id, "idempotency_key": f"cb-{uuid.uuid4()}", "dynamic_vars": {}},
        headers=_OP,
    )
    assert r.status_code == 202
    return r.json()["id"]


def test_schedule_callback_ok(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/schedule_callback",
        json={
            "call_id": call_id,
            "requested_time_text": "tomorrow afternoon",
            "requested_at": "2026-06-10T15:00:00Z",
            "notes": "prefers afternoons",
        },
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert isinstance(r.json()["id"], int)


def test_schedule_callback_minimal_no_iso_time(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": call_id, "requested_time_text": "sometime soon"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert isinstance(r.json()["id"], int)


def test_schedule_callback_requires_token(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": call_id, "requested_time_text": "soon"},
    )
    assert r.status_code == 401


def test_schedule_callback_mismatch_403(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": call_id, "requested_time_text": "soon"},
        headers=_auth(str(uuid.uuid4())),
    )
    assert r.status_code == 403


def test_schedule_callback_unknown_call_404(client, mock_dispatch):
    cid = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": cid, "requested_time_text": "soon"},
        headers=_auth(cid),
    )
    assert r.status_code == 404


def test_schedule_callback_empty_time_text_422(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": call_id, "requested_time_text": ""},
        headers=_auth(call_id),
    )
    assert r.status_code == 422


def test_schedule_callback_increments_metric(client, mock_dispatch):
    from usan_api.observability.custom_metrics import CALLBACK_REQUESTS_TOTAL

    before = CALLBACK_REQUESTS_TOTAL._value.get()
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/schedule_callback",
        json={"call_id": call_id, "requested_time_text": "soon"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert CALLBACK_REQUESTS_TOTAL._value.get() == before + 1
```

- [ ] **Step 2: Run test, verify it FAILS**
  `cd apps/api && uv run pytest tests/test_schedule_callback_endpoint.py -v`
  **Expected RED:** `test_schedule_callback_increments_metric` fails at `ImportError: cannot import name 'CALLBACK_REQUESTS_TOTAL'`; the endpoint tests return `404`/route-not-found (`405`/no matching route) because `/v1/tools/schedule_callback` is unregistered — `assert r.status_code == 200` fails.

- [ ] **Step 3: Implement**

In `apps/api/src/usan_api/observability/custom_metrics.py`, add immediately after the `TOOL_CALLS_TOTAL` block (after line 43):

```python
# No labels: a single global counter of callback requests recorded by schedule_callback.
# PHI-free by construction — requested_time_text / notes are NEVER label values (spec §9).
CALLBACK_REQUESTS_TOTAL = Counter(
    "usan_callback_requests",
    "Callback requests created.",
)
```

In `apps/api/src/usan_api/routers/tools.py`:

1. Add the repo import alongside the other `repositories` imports (after line 20, the `wellness as wellness_repo` line):

```python
from usan_api.repositories import callback_requests as callback_requests_repo
```

2. Add the metric import to the `observability.custom_metrics` import on line 14:

```python
from usan_api.observability.custom_metrics import CALLBACK_REQUESTS_TOTAL, CALLS_TOTAL, track_tool
```

3. Add the two schemas to the `usan_api.schemas.tools` import block (lines 21-34):

```python
from usan_api.schemas.tools import (
    CallbackScheduledResponse,
    CallEndedResponse,
    EndCallRequest,
    GetTodayMedsRequest,
    LoggedResponse,
    LogMedicationRequest,
    LogMetricsRequest,
    LogTranscriptRequest,
    LogWellnessRequest,
    MedicationScheduleItem,
    MetricsAcceptedResponse,
    ScheduleCallbackRequest,
    TodayMedsResponse,
    TranscriptLoggedResponse,
)
```

4. Add the endpoint after `log_medication` (after line 97, before `get_today_meds`):

```python
@router.post("/schedule_callback", response_model=CallbackScheduledResponse)
@track_tool("schedule_callback")
async def schedule_callback(
    body: ScheduleCallbackRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> CallbackScheduledResponse:
    call = await _authorize_call(body.call_id, claims, db)
    elder_id = _require_elder(call)
    row = await callback_requests_repo.create_callback_request(
        db,
        call_id=call.id,
        elder_id=elder_id,
        requested_time_text=body.requested_time_text,
        requested_at=body.requested_at,
        notes=body.notes,
    )
    await db.commit()
    # Increment AFTER commit so a crash mid-commit can't double-count (spec §7).
    # No label carries requested_time_text/notes (free-text PHI) — bounded by design.
    CALLBACK_REQUESTS_TOTAL.inc()
    # Don't log requested_time_text/notes: free-text the LLM fills. Already persisted to
    # the DB; the log keeps only call_id.
    logger.bind(call_id=str(call.id)).info("Scheduled callback request")
    return CallbackScheduledResponse(id=row.id)
```

- [ ] **Step 4: Run test, verify PASS**
  `cd apps/api && uv run pytest tests/test_schedule_callback_endpoint.py -v && ruff check src/usan_api/routers/tools.py src/usan_api/observability/custom_metrics.py tests/test_schedule_callback_endpoint.py && ruff format src/usan_api/routers/tools.py src/usan_api/observability/custom_metrics.py tests/test_schedule_callback_endpoint.py && uv run mypy src/usan_api/routers/tools.py src/usan_api/observability/custom_metrics.py`

- [ ] **Step 5: Commit**
  `git add apps/api/src/usan_api/routers/tools.py apps/api/src/usan_api/observability/custom_metrics.py apps/api/tests/test_schedule_callback_endpoint.py && git commit -m "feat(api): POST /v1/tools/schedule_callback + usan_callback_requests metric (Phase 3 Part C)"`

---

### Task C4: `schemas/admin_tools.py` — `CallbackRequestSummary` (ADD to existing file)

**Files:**
- Modify: `apps/api/src/usan_api/schemas/admin_tools.py` (file **created in Part B** — ADD this model; do NOT recreate the file — R7)
- Test: `apps/api/tests/test_admin_tools_schemas.py` (ADD a test; this file is created in Part B — add the callback case)

`CallbackRequestSummary` uses `ConfigDict(from_attributes=True)` (per contract) so it serializes directly from the ORM `CallbackRequest` row. Includes PHI (`notes`) — surfaced only through the session-gated, audited admin endpoint (Task C5). **Requires Part B migration 0011 + `CallbackRequest` model** (R10).

- [ ] **Step 1: Write the failing test** → append to `apps/api/tests/test_admin_tools_schemas.py`

```python
import uuid
from datetime import UTC, datetime

from usan_api.schemas.admin_tools import CallbackRequestSummary


def test_callback_request_summary_from_attributes():
    class _Row:
        id = 7
        call_id = uuid.uuid4()
        elder_id = uuid.uuid4()
        requested_time_text = "tomorrow afternoon"
        requested_at = datetime(2026, 6, 10, 15, 0, tzinfo=UTC)
        notes = "prefers afternoons"
        status = "open"
        created_at = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)

    row = _Row()
    summary = CallbackRequestSummary.model_validate(row)
    assert summary.id == 7
    assert summary.call_id == row.call_id
    assert summary.elder_id == row.elder_id
    assert summary.requested_time_text == "tomorrow afternoon"
    assert summary.requested_at == row.requested_at
    assert summary.notes == "prefers afternoons"
    assert summary.status == "open"
    assert summary.created_at == row.created_at


def test_callback_request_summary_allows_null_requested_at_and_notes():
    class _Row:
        id = 8
        call_id = uuid.uuid4()
        elder_id = uuid.uuid4()
        requested_time_text = "soon"
        requested_at = None
        notes = None
        status = "open"
        created_at = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)

    summary = CallbackRequestSummary.model_validate(_Row())
    assert summary.requested_at is None
    assert summary.notes is None
```

- [ ] **Step 2: Run test, verify it FAILS**
  `cd apps/api && uv run pytest tests/test_admin_tools_schemas.py -k callback_request_summary -v`
  **Expected RED:** `ImportError: cannot import name 'CallbackRequestSummary' from 'usan_api.schemas.admin_tools'` (model not yet added to the Part-B file).

- [ ] **Step 3: Implement** → ADD to `apps/api/src/usan_api/schemas/admin_tools.py` (the file already imports `uuid`, `datetime`, `BaseModel`, `ConfigDict` from Part B — reuse them; do NOT re-add imports if present):

```python
class CallbackRequestSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    call_id: uuid.UUID
    elder_id: uuid.UUID
    requested_time_text: str
    requested_at: datetime | None
    notes: str | None
    status: str
    created_at: datetime
```

- [ ] **Step 4: Run test, verify PASS**
  `cd apps/api && uv run pytest tests/test_admin_tools_schemas.py -k callback_request_summary -v && ruff check src/usan_api/schemas/admin_tools.py tests/test_admin_tools_schemas.py && ruff format src/usan_api/schemas/admin_tools.py tests/test_admin_tools_schemas.py && uv run mypy src/usan_api/schemas/admin_tools.py`

- [ ] **Step 5: Commit**
  `git add apps/api/src/usan_api/schemas/admin_tools.py apps/api/tests/test_admin_tools_schemas.py && git commit -m "feat(api): CallbackRequestSummary admin schema (Phase 3 Part C)"`

---

### Task C5: `routers/admin_tools.py` — `GET /v1/admin/callback-requests` (ADD route to existing file)

**Files:**
- Modify: `apps/api/src/usan_api/routers/admin_tools.py` (router **created & registered in Part B** with `prefix="/v1/admin"` + `dependencies=[Depends(require_admin_session)]` — ADD this route only; do NOT re-create or re-register — R7)
- Test: `apps/api/tests/test_admin_callback_requests_api.py`

Mirrors `admin_elders.py`: session-gated `GET`, `Query`-validated filters, returns `list[CallbackRequestSummary]`. Uses the cookie-jar `admin_session` fixture EXACTLY as `test_admin_elders_api`/`test_variable_catalog_api` do — the fixture sets the cookie on the shared `client`, so requests pass **no** `cookies=`/`headers=` arg. **Requires Part B migration 0011 + `CallbackRequest` model + the Part-B `admin_tools` router** (R10).

- [ ] **Step 1: Write the failing test** → `apps/api/tests/test_admin_callback_requests_api.py`

```python
import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


async def _seed_callback(
    async_database_url: str, *, requested_time_text: str, status: str = "open"
) -> tuple[str, str]:
    elder_id = str(uuid.uuid4())
    call_id = str(uuid.uuid4())
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO elders (id, name, phone_e164, timezone) "
                    "VALUES (CAST(:id AS uuid), 'Ada', :p, 'UTC')"
                ),
                {"id": elder_id, "p": f"+1555{str(uuid.UUID(elder_id).int)[:7]}"},
            )
            await conn.execute(
                text(
                    "INSERT INTO calls (id, elder_id, direction, status) "
                    "VALUES (CAST(:cid AS uuid), CAST(:eid AS uuid), 'outbound', 'completed')"
                ),
                {"cid": call_id, "eid": elder_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO callback_requests "
                    "(call_id, elder_id, requested_time_text, status) "
                    "VALUES (CAST(:cid AS uuid), CAST(:eid AS uuid), :t, :s)"
                ),
                {"cid": call_id, "eid": elder_id, "t": requested_time_text, "s": status},
            )
    finally:
        await engine.dispose()
    return call_id, elder_id


def test_callback_requests_requires_session(client):
    assert client.get("/v1/admin/callback-requests").status_code == 401


def test_list_callback_requests(client, admin_session, async_database_url):
    asyncio.run(_seed_callback(async_database_url, requested_time_text="tomorrow at 3"))
    rows = client.get("/v1/admin/callback-requests").json()
    assert any(r["requested_time_text"] == "tomorrow at 3" for r in rows)
    one = next(r for r in rows if r["requested_time_text"] == "tomorrow at 3")
    assert set(one.keys()) == {
        "id",
        "call_id",
        "elder_id",
        "requested_time_text",
        "requested_at",
        "notes",
        "status",
        "created_at",
    }
    assert one["status"] == "open"


def test_list_callback_requests_filters_by_status(client, admin_session, async_database_url):
    asyncio.run(
        _seed_callback(async_database_url, requested_time_text="open-one", status="open")
    )
    asyncio.run(
        _seed_callback(async_database_url, requested_time_text="done-one", status="resolved")
    )
    open_rows = client.get("/v1/admin/callback-requests?status=open").json()
    texts = {r["requested_time_text"] for r in open_rows}
    assert "open-one" in texts
    assert "done-one" not in texts
    assert all(r["status"] == "open" for r in open_rows)


def test_list_callback_requests_over_cap_limit_422(client, admin_session):
    assert client.get("/v1/admin/callback-requests?limit=100000").status_code == 422
```

- [ ] **Step 2: Run test, verify it FAILS**
  `cd apps/api && uv run pytest tests/test_admin_callback_requests_api.py -v`
  **Expected RED:** `test_list_callback_requests` (and the status-filter test) get `404` from the unregistered `/v1/admin/callback-requests` route → `assert any(...)` fails on an empty/`detail` body; `test_list_callback_requests_over_cap_limit_422` gets `404` instead of `422`.

- [ ] **Step 3: Implement** → ADD to `apps/api/src/usan_api/routers/admin_tools.py` (router + `require_admin_session` dependency already exist from Part B). Ensure these imports exist at the top of the Part-B file (add any missing — `Query` and the repo/summary symbols):

```python
from fastapi import Query

from usan_api.repositories import callback_requests as callback_requests_repo
from usan_api.schemas.admin_tools import CallbackRequestSummary
```

Add the route to the existing `router`:

```python
@router.get("/callback-requests", response_model=list[CallbackRequestSummary])
async def list_callback_requests(
    status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[CallbackRequestSummary]:
    # Paged + status-filtered in SQL (never select the whole table). Callback notes are
    # PHI but stay in our DB; this endpoint is session-gated via the router dependency.
    rows = await callback_requests_repo.list_callback_requests(db, status=status, limit=limit)
    return [CallbackRequestSummary.model_validate(r) for r in rows]
```

> **Note (R7):** `db: AsyncSession = Depends(get_db)`, `require_admin_session` (router-level dep), and `AsyncSession` import are already present in the Part-B `admin_tools.py`. Add only `Query` + the two new imports above if not already imported. Do not add a second `require_admin_session` (it is the router-level dependency).

- [ ] **Step 4: Run test, verify PASS**
  `cd apps/api && uv run pytest tests/test_admin_callback_requests_api.py -v && ruff check src/usan_api/routers/admin_tools.py tests/test_admin_callback_requests_api.py && ruff format src/usan_api/routers/admin_tools.py tests/test_admin_callback_requests_api.py && uv run mypy src/usan_api/routers/admin_tools.py`

- [ ] **Step 5: Commit**
  `git add apps/api/src/usan_api/routers/admin_tools.py apps/api/tests/test_admin_callback_requests_api.py && git commit -m "feat(api): GET /v1/admin/callback-requests (Phase 3 Part C)"`

---

### Task C6: Agent `schedule_callback` — `@function_tool` + `_do_schedule_callback` + `_TOOL_REGISTRY` insert + `api_client.schedule_callback`

**Files:**
- Modify: `services/agent/src/usan_agent/api_client.py` (add `schedule_callback` after `log_medication`, ~line 96)
- Modify: `services/agent/src/usan_agent/check_in.py` (add `_do_schedule_callback` after `_do_log_medication` ~line 85; add `@function_tool schedule_callback` after `log_medication` tool ~line 175; insert ONE registry line before `"end_call": end_call,` at line 199 — R5)
- Test: append to `services/agent/tests/test_api_client_tools.py` and `services/agent/tests/test_check_in.py`

Mirrors the existing pattern: `_do_*` wraps `api_client` in try/except → `logger.bind(call_id=...).warning(...)` → calm spoken fallback; success → short confirmation (R6 — Part C adds ONLY `schedule_callback`'s three pieces). The agent passes `requested_at` through as a string; the API parses it to `datetime | None`. **Registry insert is a single additive line before `end_call`** (R5). Part C does **not** touch `_select_tools`, `TOOL_NAMES`, or the four-tool tests (R3/R1/R4).

- [ ] **Step 1: Write the failing tests**

Append to `services/agent/tests/test_api_client_tools.py`:

```python
async def test_schedule_callback_posts_scoped_request(fake_http):
    _FakeClient.json_data = {"id": 11}
    await api_client.schedule_callback(
        "call-1",
        _settings(),
        requested_time_text="tomorrow afternoon",
        requested_at="2026-06-10T15:00:00Z",
        notes="prefers afternoons",
    )
    cap = fake_http.captured
    assert cap["url"] == "http://api:8000/v1/tools/schedule_callback"
    assert cap["json"] == {
        "call_id": "call-1",
        "requested_time_text": "tomorrow afternoon",
        "requested_at": "2026-06-10T15:00:00Z",
        "notes": "prefers afternoons",
    }
    assert cap["headers"]["Authorization"].startswith("Bearer ")


async def test_schedule_callback_passes_null_optionals(fake_http):
    _FakeClient.json_data = {"id": 12}
    await api_client.schedule_callback(
        "call-1", _settings(), requested_time_text="soon", requested_at=None, notes=None
    )
    assert fake_http.captured["json"] == {
        "call_id": "call-1",
        "requested_time_text": "soon",
        "requested_at": None,
        "notes": None,
    }
```

Append to `services/agent/tests/test_check_in.py`:

```python
async def test_do_schedule_callback_calls_api_and_acks(monkeypatch):
    spy = AsyncMock()
    monkeypatch.setattr(check_in.api_client, "schedule_callback", spy)
    result = await check_in._do_schedule_callback(
        _data(),
        requested_time_text="tomorrow afternoon",
        requested_at="2026-06-10T15:00:00Z",
        notes="prefers afternoons",
    )
    spy.assert_awaited_once()
    kwargs = spy.await_args.kwargs
    assert kwargs == {
        "requested_time_text": "tomorrow afternoon",
        "requested_at": "2026-06-10T15:00:00Z",
        "notes": "prefers afternoons",
    }
    assert spy.await_args.args[0] == "call-1"
    assert isinstance(result, str)
    assert result  # a spoken acknowledgement


async def test_do_schedule_callback_handles_api_failure(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(check_in.api_client, "schedule_callback", _boom)
    result = await check_in._do_schedule_callback(
        _data(), requested_time_text="soon", requested_at=None, notes=None
    )
    assert isinstance(result, str)
    assert result  # graceful string, no exception


def test_schedule_callback_in_tool_registry():
    assert "schedule_callback" in check_in._TOOL_REGISTRY


def test_select_tools_includes_schedule_callback_when_enabled():
    from usan_agent.agent_config import ToolsConfig

    tools = check_in._select_tools(ToolsConfig(enabled=["schedule_callback", "end_call"]))
    ids = {t.id for t in tools}
    assert "schedule_callback" in ids
    assert "end_call" in ids  # always force-included
```

> **Note (R3):** `_select_tools` is owned by Part A and takes a `ToolsConfig` (it reads `tools.enabled` and `getattr(tools, "sms", None)`); do NOT modify it here. The `_TOOL_REGISTRY` membership test (`test_schedule_callback_in_tool_registry`) is the signature-independent primary RED gate for this task.

- [ ] **Step 2: Run tests, verify they FAIL**
  `cd services/agent && uv run pytest tests/test_api_client_tools.py -k schedule_callback tests/test_check_in.py -k "schedule_callback" -v`
  **Expected RED:** `AttributeError: module 'usan_agent.api_client' has no attribute 'schedule_callback'`; `AttributeError: ... 'check_in' has no attribute '_do_schedule_callback'`; `test_schedule_callback_in_tool_registry` fails (`"schedule_callback" not in _TOOL_REGISTRY`).

- [ ] **Step 3: Implement**

In `services/agent/src/usan_agent/api_client.py`, add after `log_medication` (after line 95):

```python
async def schedule_callback(
    call_id: str,
    settings: Settings,
    *,
    requested_time_text: str,
    requested_at: str | None,
    notes: str | None,
) -> None:
    await _post_tool(
        "schedule_callback",
        call_id,
        settings,
        {
            "requested_time_text": requested_time_text,
            "requested_at": requested_at,
            "notes": notes,
        },
    )
```

In `services/agent/src/usan_agent/check_in.py`:

1. Add `_do_schedule_callback` after `_do_log_medication` (after line 84):

```python
async def _do_schedule_callback(
    data: CheckInData,
    *,
    requested_time_text: str,
    requested_at: str | None = None,
    notes: str | None = None,
) -> str:
    try:
        await api_client.schedule_callback(
            data.call_id,
            data.settings,
            requested_time_text=requested_time_text,
            requested_at=requested_at,
            notes=notes,
        )
    except Exception:
        logger.bind(call_id=data.call_id).warning("schedule_callback tool failed")
        return "I had trouble noting that callback time, but we can still continue."
    return "Of course, I've noted that you'd like a call back then."
```

2. Add the `@function_tool` after the `log_medication` tool (after line 174, before `get_today_meds`):

```python
@function_tool
async def schedule_callback(
    ctx: RunContext[CheckInData],
    requested_time_text: str,
    requested_at: str | None = None,
    notes: str | None = None,
) -> str:
    """Record that the elder would like a call back at a particular time.

    This does not place a call; it stores a request for a human to action.

    Args:
        requested_time_text: The elder's own words for when they'd like the call back.
        requested_at: Optional best-effort ISO-8601 timestamp; omit if you can't resolve one.
        notes: Optional short free-text note about the request.
    """
    return await _do_schedule_callback(
        ctx.userdata,
        requested_time_text=requested_time_text,
        requested_at=requested_at,
        notes=notes,
    )
```

3. Insert ONE line into `_TOOL_REGISTRY` immediately **before** the `"end_call": end_call,` line (R5). The dict becomes:

```python
_TOOL_REGISTRY: dict[str, Any] = {
    "log_wellness": log_wellness,
    "log_medication": log_medication,
    "get_today_meds": get_today_meds,
    "schedule_callback": schedule_callback,
    "end_call": end_call,
}
```

> If Part B already inserted `"flag_for_followup": flag_for_followup,` before `end_call`, the C insert goes immediately after that line, still before `"end_call": end_call,` — single additive line, no rewrite of the dict (R5).

- [ ] **Step 4: Run tests, verify PASS**
  `cd services/agent && uv run pytest tests/test_api_client_tools.py tests/test_check_in.py -v && ruff check src/usan_agent/check_in.py src/usan_agent/api_client.py tests/test_api_client_tools.py tests/test_check_in.py && uv run mypy src/usan_agent/check_in.py src/usan_agent/api_client.py`

  > Run the **full** `test_check_in.py` (not just `-k schedule_callback`) to confirm the Part-A registry-driven four-tool tests (R4) stay green after the additive registry insert.

- [ ] **Step 5: Commit**
  `git add services/agent/src/usan_agent/check_in.py services/agent/src/usan_agent/api_client.py services/agent/tests/test_api_client_tools.py services/agent/tests/test_check_in.py && git commit -m "feat(agent): schedule_callback function_tool + api_client (Phase 3 Part C)"`

---

**Part C summary of touched files (all additive):**
- `apps/api/src/usan_api/repositories/callback_requests.py` (new)
- `apps/api/src/usan_api/schemas/tools.py` (+2 models)
- `apps/api/src/usan_api/observability/custom_metrics.py` (+`CALLBACK_REQUESTS_TOTAL`)
- `apps/api/src/usan_api/routers/tools.py` (+`POST /schedule_callback`)
- `apps/api/src/usan_api/schemas/admin_tools.py` (+`CallbackRequestSummary`, Part-B file)
- `apps/api/src/usan_api/routers/admin_tools.py` (+`GET /v1/admin/callback-requests`, Part-B file)
- `services/agent/src/usan_agent/api_client.py` (+`schedule_callback`)
- `services/agent/src/usan_agent/check_in.py` (+`_do_schedule_callback`, +`@function_tool`, +1 registry line)
- Tests: `test_callback_requests_repo.py`, `test_tools_schemas.py`, `test_schedule_callback_endpoint.py`, `test_admin_tools_schemas.py`, `test_admin_callback_requests_api.py` (new), `test_api_client_tools.py` & `test_check_in.py` (appended)

**Untouched per R1–R7:** `TOOL_NAMES`, `_select_tools`, `ToolsConfig` default `enabled`, the four-tool agent tests, and `admin_tools` router registration in `main.py` — all owned by Part A/B.

---

## Part D — send_sms (additive) + genuine gates + sync test

The `_blank_to_none` validator lists Python field names (e.g. `telnyx_caller_id`), not aliases. Good — Part D adds `telnyx_messaging_api_key`, `telnyx_messaging_profile_id`, `telnyx_from_number` to that list.

I have everything I need. Now I'll emit Part D.

# Part D — `send_sms` (additive) + genuine gates + sync test

> **Depends-on (R10):** D6, D8, D9, D10, D12 use DB tables `sms_messages` — they **require Part B migration 0011 + the `SmsMessage` model in `db/models.py`**, plus Part B's `repositories/sms_messages.py` (created in Part B per the SHARED CONTRACT). D8/D12 also assume Part A's `tool_catalog.py` (`send_sms` in `TOOL_CATALOG`) and Part B's `routers/admin_tools.py` + `schemas/admin_tools.py` already exist and are registered in `main.py`. Execute strictly after A→B→C.
>
> **Ownership (R1–R7):** Part D does **not** touch `TOOL_NAMES`, `ToolsConfig.enabled` `default_factory`, `_select_tools` (the R3 `getattr(tools, "sms", None)` guard already drops `send_sms` when no templates exist), the four-tool agent tests, or re-register `admin_tools`. Part D adds only `send_sms`'s own pieces.

---

### Task D1: API-side `SmsTemplate` / `SmsToolConfig` + `ToolsConfig.sms` + PHI hard-block validator

**Files:**
- Modify `apps/api/src/usan_api/schemas/agent_config.py` (add classes before `ToolsConfig` at line 174; add `sms` field + `_sms_templates_no_phi` validator inside `ToolsConfig`).
- Test `apps/api/tests/test_agent_config_schema.py` (append).

- [ ] Step 1: Write the failing test

```python
# apps/api/tests/test_agent_config_schema.py  (append)
import pytest
from pydantic import ValidationError

from usan_api.schemas.agent_config import (
    AgentConfig,
    DEFAULT_AGENT_CONFIG,
    SmsTemplate,
    SmsToolConfig,
    ToolsConfig,
)


def test_sms_template_accepts_non_phi_tokens():
    cfg = ToolsConfig(
        enabled=list(DEFAULT_AGENT_CONFIG.tools.enabled),
        sms=SmsToolConfig(
            templates=[
                SmsTemplate(
                    key="med_reminder",
                    label="Med reminder",
                    body="Hello {{first_name}}, this is your USAN reminder for {{current_date}}.",
                )
            ]
        ),
    )
    assert cfg.sms is not None
    assert cfg.sms.templates[0].key == "med_reminder"


def test_sms_default_is_none():
    assert ToolsConfig().sms is None


@pytest.mark.parametrize(
    "token", ["last_check_in", "last_check_in_line", "last_mood", "last_pain", "today_meds"]
)
def test_sms_template_phi_token_hard_blocks(token):
    with pytest.raises(ValidationError) as exc:
        SmsToolConfig(
            templates=[
                SmsTemplate(key="bad", label="Bad", body="Your status: {{" + token + "}}")
            ]
        )
    # the validator runs on ToolsConfig too:
    with pytest.raises(ValidationError):
        ToolsConfig(
            sms={"templates": [{"key": "bad", "label": "Bad", "body": "x {{" + token + "}}"}]}
        )
    assert "protected health information" in str(exc.value).lower() or "phi" in str(exc.value).lower()


def test_sms_template_key_slug_enforced():
    with pytest.raises(ValidationError):
        SmsTemplate(key="Bad Key!", label="x", body="hello")


def test_sms_config_roundtrips_through_agent_config():
    base = DEFAULT_AGENT_CONFIG.model_dump()
    base["tools"] = {
        "enabled": list(DEFAULT_AGENT_CONFIG.tools.enabled),
        "sms": {"templates": [{"key": "a", "label": "A", "body": "Hi {{first_name}}"}]},
    }
    cfg = AgentConfig.model_validate(base)
    assert cfg.tools.sms is not None
    assert cfg.tools.sms.templates[0].key == "a"
```

> Note: the `SmsToolConfig`-level PHI raise requires the validator to live on `SmsToolConfig` *and* be re-checked when `ToolsConfig` builds `sms` from a dict. We place the validator on `ToolsConfig` (mode="after") which sees the constructed `SmsToolConfig`. To also catch direct `SmsToolConfig(...)` construction, the validator is mirrored as a `SmsToolConfig` model_validator. The SHARED CONTRACT names it `_sms_templates_no_phi` on `ToolsConfig`; we put the canonical hard-block there and a thin one on `SmsToolConfig` so both construction paths raise. (Both raise the same PHI message.)

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_agent_config_schema.py -k "sms" -v
```
RED: `ImportError: cannot import name 'SmsTemplate'` (the classes and `ToolsConfig.sms` do not exist yet).

- [ ] Step 3: Implement

In `apps/api/src/usan_api/schemas/agent_config.py`, the import on line 17 already pulls `PHI_BUILTIN_NAMES`. Add the SMS classes immediately **before** `class ToolsConfig(BaseModel):` (line 174):

```python
# --- send_sms templates (Phase 3 §6.1) -------------------------------------
# Operator-authored SMS bodies the LLM selects by key (never free text). A body
# may reference ONLY non-PHI catalog variables: a PHI token (PHI_BUILTIN_NAMES)
# hard-blocks save (HTTP 422), stricter than the greeting warn-only rule, because
# SMS leaves our system unencrypted and carrier-visible (design §6.2). Token
# detection reuses the Phase 2 _TOKEN_RE so the two layers agree on what a token is.
class SmsTemplate(BaseModel):
    key: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    label: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=480)


def _phi_tokens_in_body(body: str) -> list[str]:
    """PHI catalog tokens used in an SMS body, de-duplicated in first-seen order."""
    seen: list[str] = []
    for name in _TOKEN_RE.findall(body):
        if name in PHI_BUILTIN_NAMES and name not in seen:
            seen.append(name)
    return seen


def _reject_phi_in_templates(templates: list["SmsTemplate"]) -> None:
    for tmpl in templates:
        phi = _phi_tokens_in_body(tmpl.body)
        if phi:
            joined = ", ".join("{{" + n + "}}" for n in phi)
            raise ValueError(
                f"SMS template '{tmpl.key}' body references protected health information "
                f"({joined}); SMS bodies may use non-PHI variables only"
            )


class SmsToolConfig(BaseModel):
    templates: list[SmsTemplate] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_phi_in_bodies(self) -> "SmsToolConfig":
        _reject_phi_in_templates(self.templates)
        return self
```

Then inside `ToolsConfig` (after the `enabled` field and its `_known_tools` validator), add the `sms` field and validator:

```python
class ToolsConfig(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: [
            "log_wellness",
            "log_medication",
            "get_today_meds",
            "flag_for_followup",
            "schedule_callback",
            "send_sms",
            "end_call",
        ]
    )
    sms: SmsToolConfig | None = None

    @field_validator("enabled")
    @classmethod
    def _known_tools(cls, v: list[str]) -> list[str]:
        bad = [t for t in v if t not in TOOL_NAMES]
        if bad:
            raise ValueError(f"unknown tool(s): {', '.join(sorted(set(bad)))}")
        return v

    @model_validator(mode="after")
    def _sms_templates_no_phi(self) -> "ToolsConfig":
        # HARD BLOCK (design §6.2): a PHI token in any SMS body fails to save (422).
        if self.sms is not None:
            _reject_phi_in_templates(self.sms.templates)
        return self
```

> The `enabled` `default_factory` (7 names) is **owned by Part A (R2)** — Part D does not introduce it; it is shown here only because it is already present when Part D runs. Part D adds only the `sms` field, `_sms_templates_no_phi`, and the SMS classes.

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_agent_config_schema.py -k "sms" -v && ruff check src/usan_api/schemas/agent_config.py && uv run mypy src/usan_api/schemas/agent_config.py
```

- [ ] Step 5: Commit

```bash
git add apps/api/src/usan_api/schemas/agent_config.py apps/api/tests/test_agent_config_schema.py
git commit -m "feat(api): SMS template config + PHI hard-block validator (Phase 3 send_sms)"
```

---

### Task D2: Agent-side `SmsTemplate` / `SmsToolConfig` mirror + `ToolsConfig.sms` (NO validators)

**Files:**
- Modify `services/agent/src/usan_agent/agent_config.py` (add classes before `ToolsConfig` line 52; add `sms` field).
- Test `services/agent/tests/test_agent_config.py` (append).

- [ ] Step 1: Write the failing test

```python
# services/agent/tests/test_agent_config.py  (append)
from usan_agent.agent_config import AgentConfig, DEFAULT_AGENT_CONFIG, SmsTemplate, SmsToolConfig, ToolsConfig


def test_agent_sms_config_parses_without_validators():
    # Agent mirror is parse-only: it accepts ANY body (even a PHI token) because the
    # API already validated on the write path. No validator may reject here.
    cfg = ToolsConfig(
        enabled=list(DEFAULT_AGENT_CONFIG.tools.enabled),
        sms=SmsToolConfig(
            templates=[SmsTemplate(key="x", label="X", body="Status {{last_mood}}")]
        ),
    )
    assert cfg.sms is not None
    assert cfg.sms.templates[0].key == "x"


def test_agent_sms_default_is_none():
    assert ToolsConfig().sms is None


def test_agent_config_roundtrips_sms_block():
    base = DEFAULT_AGENT_CONFIG.model_dump()
    base["tools"] = {
        "enabled": list(DEFAULT_AGENT_CONFIG.tools.enabled),
        "sms": {"templates": [{"key": "a", "label": "A", "body": "Hi {{first_name}}"}]},
    }
    cfg = AgentConfig.model_validate(base)
    assert cfg.tools.sms is not None
    assert cfg.tools.sms.templates[0].key == "a"
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd services/agent && uv run pytest tests/test_agent_config.py -k "sms" -v
```
RED: `ImportError: cannot import name 'SmsTemplate'`.

- [ ] Step 3: Implement

In `services/agent/src/usan_agent/agent_config.py`, add before `class ToolsConfig` (line 52):

```python
class SmsTemplate(BaseModel):
    key: str
    label: str
    body: str


class SmsToolConfig(BaseModel):
    templates: list[SmsTemplate] = Field(default_factory=list)
```

And give `ToolsConfig` the `sms` field (the 7-name `enabled` default is **Part A-owned**; shown for context):

```python
class ToolsConfig(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: [
            "log_wellness",
            "log_medication",
            "get_today_meds",
            "flag_for_followup",
            "schedule_callback",
            "send_sms",
            "end_call",
        ]
    )
    sms: SmsToolConfig | None = None
```

> No validators on the agent side (mirrors the contract). The R3 `_select_tools` guard reads `getattr(tools, "sms", None)`; once this field exists `send_sms` is registered only when ≥1 template is present.

- [ ] Step 4: Run test, verify PASS

```bash
cd services/agent && uv run pytest tests/test_agent_config.py -k "sms" -v && ruff check src/usan_agent/agent_config.py && uv run mypy src/usan_agent/agent_config.py
```

- [ ] Step 5: Commit

```bash
git add services/agent/src/usan_agent/agent_config.py services/agent/tests/test_agent_config.py
git commit -m "feat(agent): mirror SMS template config (parse-only, no validators)"
```

---

### Task D3: `settings.py` Telnyx messaging fields + `_blank_to_none`

**Files:**
- Modify `apps/api/src/usan_api/settings.py` (add 6 fields after `pricing_version` line 106; extend `_blank_to_none` list lines 108-121).
- Test `apps/api/tests/test_settings.py` (append; create if absent).

- [ ] Step 1: Write the failing test

```python
# apps/api/tests/test_settings_messaging.py  (new)
import pytest

from usan_api.settings import Settings

_BASE = {
    "DATABASE_URL": "postgresql://u:p@localhost/db",
    "LIVEKIT_API_KEY": "k",
    "LIVEKIT_API_SECRET": "a" * 32,
    "LIVEKIT_URL": "ws://livekit:7880",
    "JWT_SIGNING_KEY": "s" * 32,
    "OPERATOR_API_KEY": "o" * 32,
}


def test_messaging_defaults_disabled():
    s = Settings(**_BASE)
    assert s.telnyx_messaging_enabled is False
    assert s.telnyx_messaging_api_key is None
    assert s.telnyx_messaging_profile_id is None
    assert s.telnyx_from_number is None
    assert s.telnyx_messaging_api_url == "https://api.telnyx.com/v2"
    assert s.telnyx_messaging_timeout_s == 10


def test_messaging_blank_aliases_coerce_to_none():
    s = Settings(
        **_BASE,
        TELNYX_MESSAGING_API_KEY="   ",
        TELNYX_MESSAGING_PROFILE_ID="",
        TELNYX_FROM_NUMBER="",
    )
    assert s.telnyx_messaging_api_key is None
    assert s.telnyx_messaging_profile_id is None
    assert s.telnyx_from_number is None


def test_messaging_enabled_and_secret_set():
    s = Settings(
        **_BASE,
        TELNYX_MESSAGING_ENABLED="true",
        TELNYX_MESSAGING_API_KEY="KEY123",
        TELNYX_MESSAGING_PROFILE_ID="mp1",
        TELNYX_FROM_NUMBER="+15551230000",
    )
    assert s.telnyx_messaging_enabled is True
    assert s.telnyx_messaging_api_key.get_secret_value() == "KEY123"
    assert s.telnyx_messaging_profile_id == "mp1"
    assert s.telnyx_from_number == "+15551230000"
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_settings_messaging.py -v
```
RED: `AttributeError: 'Settings' object has no attribute 'telnyx_messaging_enabled'`.

- [ ] Step 3: Implement

In `apps/api/src/usan_api/settings.py`, after `pricing_version` (line 106) add:

```python
    # --- Telnyx Messaging (Phase 3 send_sms; design §6.6). Feature flag default
    # FALSE: SMS never fires until a deploy explicitly enables it. The 3 secret/
    # profile/from fields are blank-able (compose passes "" when unset).
    telnyx_messaging_api_key: SecretStr | None = Field(
        default=None, alias="TELNYX_MESSAGING_API_KEY"
    )
    telnyx_messaging_profile_id: str | None = Field(
        default=None, alias="TELNYX_MESSAGING_PROFILE_ID"
    )
    telnyx_from_number: str | None = Field(default=None, alias="TELNYX_FROM_NUMBER")
    telnyx_messaging_enabled: bool = Field(default=False, alias="TELNYX_MESSAGING_ENABLED")
    telnyx_messaging_api_url: str = Field(
        default="https://api.telnyx.com/v2", alias="TELNYX_MESSAGING_API_URL"
    )
    telnyx_messaging_timeout_s: int = Field(
        default=10, ge=1, le=60, alias="TELNYX_MESSAGING_TIMEOUT_S"
    )
```

Extend the `_blank_to_none` validator field list (insert the 3 blank-able messaging fields after `"phi_retention_days",`, before `mode="before",`):

```python
    @field_validator(
        "telnyx_caller_id",
        "telnyx_sip_username",
        "telnyx_sip_password",
        "livekit_sip_outbound_trunk_id",
        "google_oauth_client_id",
        "google_oauth_redirect_uri",
        "google_oauth_hd",
        "phi_retention_days",
        "telnyx_messaging_api_key",
        "telnyx_messaging_profile_id",
        "telnyx_from_number",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, v: object) -> object:
```

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_settings_messaging.py -v && ruff check src/usan_api/settings.py && uv run mypy src/usan_api/settings.py
```

- [ ] Step 5: Commit

```bash
git add apps/api/src/usan_api/settings.py apps/api/tests/test_settings_messaging.py
git commit -m "feat(api): Telnyx messaging settings (feature-flag default off)"
```

---

### Task D4: `telnyx_messaging.py` client + `TelnyxMessagingError`

**Files:**
- Create `apps/api/src/usan_api/telnyx_messaging.py`.
- Test `apps/api/tests/test_telnyx_messaging.py` (new).

- [ ] Step 1: Write the failing test (httpx mocked, ok + error)

```python
# apps/api/tests/test_telnyx_messaging.py  (new)
import pytest

from usan_api import telnyx_messaging
from usan_api.settings import Settings

_BASE = {
    "DATABASE_URL": "postgresql://u:p@localhost/db",
    "LIVEKIT_API_KEY": "k",
    "LIVEKIT_API_SECRET": "a" * 32,
    "LIVEKIT_URL": "ws://livekit:7880",
    "JWT_SIGNING_KEY": "s" * 32,
    "OPERATOR_API_KEY": "o" * 32,
}


def _settings() -> Settings:
    return Settings(
        **_BASE,
        TELNYX_MESSAGING_API_KEY="KEY123",
        TELNYX_MESSAGING_PROFILE_ID="mp1",
        TELNYX_FROM_NUMBER="+15551230000",
    )


class _Resp:
    def __init__(self, payload, *, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, resp, captured):
        self._resp = resp
        self._captured = captured

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json, headers):
        self._captured["url"] = url
        self._captured["json"] = json
        self._captured["headers"] = headers
        return self._resp


@pytest.mark.asyncio
async def test_send_sms_success_returns_message_id(monkeypatch):
    captured: dict = {}
    resp = _Resp({"data": {"id": "msg-abc"}})
    monkeypatch.setattr(
        telnyx_messaging.httpx, "AsyncClient", lambda timeout: _FakeClient(resp, captured)
    )
    mid = await telnyx_messaging.send_sms(
        _settings(), to_number="+15557654321", body="Hello there"
    )
    assert mid == "msg-abc"
    assert captured["url"] == "https://api.telnyx.com/v2/messages"
    assert captured["json"] == {
        "messaging_profile_id": "mp1",
        "from": "+15551230000",
        "to": "+15557654321",
        "text": "Hello there",
    }
    assert captured["headers"]["Authorization"] == "Bearer KEY123"


@pytest.mark.asyncio
async def test_send_sms_http_error_wrapped(monkeypatch):
    import httpx

    err = httpx.HTTPStatusError("400", request=None, response=None)
    resp = _Resp({}, raise_exc=err)
    monkeypatch.setattr(
        telnyx_messaging.httpx, "AsyncClient", lambda timeout: _FakeClient(resp, {})
    )
    with pytest.raises(telnyx_messaging.TelnyxMessagingError):
        await telnyx_messaging.send_sms(_settings(), to_number="+1555", body="x")


@pytest.mark.asyncio
async def test_send_sms_missing_id_raises(monkeypatch):
    resp = _Resp({"data": {}})
    monkeypatch.setattr(
        telnyx_messaging.httpx, "AsyncClient", lambda timeout: _FakeClient(resp, {})
    )
    with pytest.raises(telnyx_messaging.TelnyxMessagingError):
        await telnyx_messaging.send_sms(_settings(), to_number="+1555", body="x")
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_telnyx_messaging.py -v
```
RED: `ModuleNotFoundError: No module named 'usan_api.telnyx_messaging'`.

- [ ] Step 3: Implement

```python
# apps/api/src/usan_api/telnyx_messaging.py  (new)
"""Telnyx Messaging API client (Phase 3 send_sms; design §6.5).

Mirrors oauth.py's raw-httpx + wrap-errors pattern (no SDK). One function:
``send_sms`` POSTs to /messages with the configured messaging profile and from
number, returns the Telnyx message id, and wraps any transport/HTTP/parse failure
in TelnyxMessagingError. The caller (sms_outbox) marks the row failed on raise.
"""

from typing import Any, cast

import httpx

from usan_api.settings import Settings


class TelnyxMessagingError(Exception):
    """Any failure sending an SMS via the Telnyx Messaging API."""


async def send_sms(settings: Settings, *, to_number: str, body: str) -> str:
    """Send one SMS; return the Telnyx message id. Raises TelnyxMessagingError.

    Requires telnyx_messaging_api_key / _profile_id / _from_number to be set (the
    caller gates on the feature flag, but a misconfigured flag-on/secret-missing
    combination still raises rather than silently sending half a request).
    """
    api_key = settings.telnyx_messaging_api_key
    if api_key is None or not settings.telnyx_messaging_profile_id or not settings.telnyx_from_number:
        raise TelnyxMessagingError("Telnyx messaging is not fully configured")
    url = f"{settings.telnyx_messaging_api_url}/messages"
    headers = {"Authorization": f"Bearer {api_key.get_secret_value()}"}
    payload = {
        "messaging_profile_id": settings.telnyx_messaging_profile_id,
        "from": settings.telnyx_from_number,
        "to": to_number,
        "text": body,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.telnyx_messaging_timeout_s) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = cast(dict[str, Any], resp.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise TelnyxMessagingError("Telnyx message send failed") from exc
    message_id = (data.get("data") or {}).get("id")
    if not message_id:
        raise TelnyxMessagingError("Telnyx response had no message id")
    return str(message_id)
```

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_telnyx_messaging.py -v && ruff check src/usan_api/telnyx_messaging.py && uv run mypy src/usan_api/telnyx_messaging.py
```

- [ ] Step 5: Commit

```bash
git add apps/api/src/usan_api/telnyx_messaging.py apps/api/tests/test_telnyx_messaging.py
git commit -m "feat(api): Telnyx Messaging client (send_sms + TelnyxMessagingError)"
```

---

### Task D5: `sms_render.py` `render_sms_body` with local value sanitize (R9a)

**Files:**
- Create `apps/api/src/usan_api/sms_render.py`.
- Test `apps/api/tests/test_sms_render.py` (new).

- [ ] Step 1: Write the failing test

```python
# apps/api/tests/test_sms_render.py  (new)
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from usan_api.sms_render import render_sms_body

_NOW = datetime(2026, 6, 9, 9, 15, 0, tzinfo=ZoneInfo("UTC"))


def _elder(name="Margaret Doe", tz="UTC", meds=None):
    return SimpleNamespace(
        name=name, timezone=tz, meta={"medication_schedule": meds or []}
    )


def _call(direction="outbound"):
    return SimpleNamespace(direction=SimpleNamespace(value=direction))


def test_renders_non_phi_token():
    out = render_sms_body(
        "Hello {{first_name}}, from USAN.", call=_call(), elder=_elder(), now=_NOW
    )
    assert out == "Hello Margaret, from USAN."


def test_phi_token_renders_empty_defense_in_depth():
    # A PHI token would be hard-blocked at save; if one ever reaches render it
    # resolves to empty (the non-PHI subset drops PHI names).
    out = render_sms_body("Mood: {{last_mood}}.", call=_call(), elder=_elder(), now=_NOW)
    assert out == "Mood: ."


def test_unknown_token_renders_empty():
    out = render_sms_body("X {{not_a_var}} Y", call=_call(), elder=_elder(), now=_NOW)
    assert out == "X  Y"


def test_value_is_sanitized_before_insertion():
    # A name carrying braces / control chars / a brace-injection is neutralized.
    elder = _elder(name="Ann\n{{evil}}")
    out = render_sms_body("Hi {{first_name}}.", call=_call(), elder=elder, now=_NOW)
    assert "{" not in out and "}" not in out
    assert "\n" not in out


def test_clock_tokens_resolve():
    out = render_sms_body("Today is {{current_date}}.", call=_call(), elder=_elder(), now=_NOW)
    assert "2026" in out or "June" in out
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_sms_render.py -v
```
RED: `ModuleNotFoundError: No module named 'usan_api.sms_render'`.

- [ ] Step 3: Implement

```python
# apps/api/src/usan_api/sms_render.py  (new)
"""Render an SMS template body with the call's NON-PHI variables (design §6.3, §9).

A template body may reference only non-PHI catalog variables (PHI tokens are
hard-blocked at save in agent_config._sms_templates_no_phi). Here, as
defense-in-depth, we (1) resolve the builtin vars, (2) DROP every PHI name, (3)
add the two runtime clock vars, (4) pass each value through a LOCAL sanitize
(strip control chars / braces / zero-width) BEFORE substitution, and (5) replace
unknown tokens with the empty string. Substitution is token-scoped via _TOKEN_RE,
never str.format, so a hostile value cannot inject a new slot.
"""

import re
from datetime import datetime
from typing import Any

from usan_api import builtin_vars
from usan_api.schemas.agent_config import _TOKEN_RE
from usan_api.schemas.variable_catalog import PHI_BUILTIN_NAMES

# Mirrors services/agent sanitize._PROMPT_UNSAFE (kept local: apps/api must not
# import services/agent). Strips format-slot braces, ASCII control chars, the
# Unicode line/paragraph separators, and invisible/directional chars.
_VALUE_UNSAFE = re.compile(
    r"[{}\x00-\x1f\x7f\x85\u00ad\u200b-\u200f\u2028-\u2029\u202a-\u202e\u2060-\u2064\ufeff]"
)
_VALUE_MAX_LEN = 160


def _sanitize(value: str) -> str:
    text = _VALUE_UNSAFE.sub(" ", value)
    text = " ".join(text.split())
    return text[:_VALUE_MAX_LEN].strip()


def _clock_vars(elder: Any, now: datetime) -> dict[str, str]:
    """current_time / current_date in the elder's timezone (best-effort)."""
    from zoneinfo import ZoneInfo

    tz = getattr(elder, "timezone", "") or "UTC"
    try:
        local = now.astimezone(ZoneInfo(tz))
    except Exception:
        local = now
    return {
        "current_time": local.strftime("%-I:%M %p").lstrip("0"),
        "current_date": local.strftime("%A, %B %-d"),
    }


def render_sms_body(template_body: str, *, call: Any, elder: Any, now: datetime | None = None) -> str:
    """Substitute non-PHI {{tokens}} in ``template_body`` for one call.

    ``now`` is injectable for testing; the endpoint calls
    ``render_sms_body(template.body, call=call, elder=elder)``.
    """
    when = now or datetime.now()
    direction = getattr(getattr(call, "direction", None), "value", "outbound")
    resolved, _tz = builtin_vars.resolve_builtin_vars(elder, None, direction=direction)
    values = {k: v for k, v in resolved.items() if k not in PHI_BUILTIN_NAMES}
    values.update(_clock_vars(elder, when))

    def _replace(match: "re.Match[str]") -> str:
        name = match.group(1)
        raw = values.get(name, "")
        return _sanitize(raw) if raw else ""

    return _TOKEN_RE.sub(_replace, template_body)
```

> `resolve_builtin_vars(elder, None, ...)` is given `last_log=None` because PHI built-ins (which derive from the wellness log) are dropped anyway — the non-PHI subset (`first_name`, `elder_name`, `call_direction`) needs only the elder + direction.

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_sms_render.py -v && ruff check src/usan_api/sms_render.py && uv run mypy src/usan_api/sms_render.py
```

- [ ] Step 5: Commit

```bash
git add apps/api/src/usan_api/sms_render.py apps/api/tests/test_sms_render.py
git commit -m "feat(api): render SMS body with non-PHI vars + value sanitize"
```

---

### Task D6: `repositories/sms_messages.py` (status-guarded `mark_sent`/`mark_failed`)

**Files:**
- Create `apps/api/src/usan_api/repositories/sms_messages.py`.
- Test `apps/api/tests/test_sms_messages_repo.py` (new).

> **Depends-on (R10):** requires Part B migration 0011 + the `SmsMessage` model.

- [ ] Step 1: Write the failing test

```python
# apps/api/tests/test_sms_messages_repo.py  (new)
import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import sms_messages as sms_repo


async def _seed_call(url):
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    try:
        async with factory() as db:
            elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
            call = await calls_repo.create_call(
                db, elder_id=elder.id, direction=CallDirection.OUTBOUND, status=CallStatus.IN_PROGRESS
            )
            await db.commit()
            return url, call.id, elder.id
    finally:
        await engine.dispose()


async def _run(url, call_id, elder_id):
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            row = await sms_repo.create_sms_message(
                db, call_id=call_id, elder_id=elder_id,
                to_number="+15557654321", template_key="t", body="hi",
            )
            await db.commit()
            assert row.status == "pending"
            pend = await sms_repo.get_pending_for_call(db, call_id)
            assert len(pend) == 1

            sent = await sms_repo.mark_sent(db, row.id, telnyx_message_id="msg-1")
            await db.commit()
            assert sent is not None and sent.status == "sent"
            assert sent.telnyx_message_id == "msg-1"

            # Idempotent: second mark_sent on an already-sent row no-ops (returns None).
            again = await sms_repo.mark_sent(db, row.id, telnyx_message_id="msg-2")
            assert again is None
            # And mark_failed on a non-pending row also no-ops.
            failed = await sms_repo.mark_failed(db, row.id, error={"reason": "x"})
            assert failed is None
            assert await sms_repo.get_pending_for_call(db, call_id) == []
    finally:
        await engine.dispose()


def test_sms_repo_create_and_status_guarded_transitions(async_database_url):
    url, call_id, elder_id = asyncio.run(_seed_call(async_database_url))
    asyncio.run(_run(url, call_id, elder_id))


async def _run_failed(url, call_id, elder_id):
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            row = await sms_repo.create_sms_message(
                db, call_id=call_id, elder_id=elder_id,
                to_number="+1", template_key="t", body="hi",
            )
            await db.commit()
            failed = await sms_repo.mark_failed(db, row.id, error={"reason": "messaging_disabled"})
            await db.commit()
            assert failed is not None and failed.status == "failed"
            assert failed.error == {"reason": "messaging_disabled"}
            msgs = await sms_repo.list_messages(db, status="failed")
            assert any(m.id == row.id for m in msgs)
    finally:
        await engine.dispose()


def test_sms_repo_mark_failed(async_database_url):
    url, call_id, elder_id = asyncio.run(_seed_call(async_database_url))
    asyncio.run(_run_failed(url, call_id, elder_id))
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_sms_messages_repo.py -v
```
RED: `ModuleNotFoundError: No module named 'usan_api.repositories.sms_messages'`.

- [ ] Step 3: Implement

```python
# apps/api/src/usan_api/repositories/sms_messages.py  (new)
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.models import SmsMessage


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def create_sms_message(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    elder_id: uuid.UUID,
    to_number: str,
    template_key: str,
    body: str,
) -> SmsMessage:
    row = SmsMessage(
        call_id=call_id,
        elder_id=elder_id,
        to_number=to_number,
        template_key=template_key,
        body=body,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row


async def get_pending_for_call(db: AsyncSession, call_id: uuid.UUID) -> list[SmsMessage]:
    result = await db.execute(
        select(SmsMessage)
        .where(SmsMessage.call_id == call_id, SmsMessage.status == "pending")
        .order_by(SmsMessage.created_at)
    )
    return list(result.scalars().all())


async def mark_sent(
    db: AsyncSession, sms_id: uuid.UUID, *, telnyx_message_id: str
) -> SmsMessage | None:
    """Status-guarded pending->sent. Returns the row, or None if it was not pending
    (idempotent: a second flush claims nothing)."""
    result = await db.execute(
        update(SmsMessage)
        .where(SmsMessage.id == sms_id, SmsMessage.status == "pending")
        .values(
            status="sent",
            telnyx_message_id=telnyx_message_id,
            sent_at=_utcnow(),
            updated_at=_utcnow(),
        )
        .returning(SmsMessage.id)
    )
    if result.scalar_one_or_none() is None:
        return None
    await db.flush()
    return await db.get(SmsMessage, sms_id)


async def mark_failed(db: AsyncSession, sms_id: uuid.UUID, *, error: dict) -> SmsMessage | None:
    """Status-guarded pending->failed. Returns the row, or None if not pending."""
    result = await db.execute(
        update(SmsMessage)
        .where(SmsMessage.id == sms_id, SmsMessage.status == "pending")
        .values(status="failed", error=error, updated_at=_utcnow())
        .returning(SmsMessage.id)
    )
    if result.scalar_one_or_none() is None:
        return None
    await db.flush()
    return await db.get(SmsMessage, sms_id)


async def list_messages(
    db: AsyncSession, *, status: str | None = None, limit: int = 100
) -> list[SmsMessage]:
    stmt = select(SmsMessage)
    if status is not None:
        stmt = stmt.where(SmsMessage.status == status)
    stmt = stmt.order_by(SmsMessage.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    return list(result.scalars().all())
```

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_sms_messages_repo.py -v && ruff check src/usan_api/repositories/sms_messages.py && uv run mypy src/usan_api/repositories/sms_messages.py
```

- [ ] Step 5: Commit

```bash
git add apps/api/src/usan_api/repositories/sms_messages.py apps/api/tests/test_sms_messages_repo.py
git commit -m "feat(api): sms_messages repo with status-guarded mark_sent/mark_failed"
```

---

### Task D7: `schemas/tools.py` `SendSmsRequest` + `SmsQueuedResponse`

**Files:**
- Modify `apps/api/src/usan_api/schemas/tools.py` (append after `MetricsAcceptedResponse` line 102).
- Test `apps/api/tests/test_tools_schemas.py` (append; create if absent).

- [ ] Step 1: Write the failing test

```python
# apps/api/tests/test_tools_schemas.py  (new or append)
import uuid

import pytest
from pydantic import ValidationError

from usan_api.schemas.tools import SendSmsRequest, SmsQueuedResponse


def test_send_sms_request_valid():
    req = SendSmsRequest(call_id=uuid.uuid4(), template_key="med_reminder")
    assert req.template_key == "med_reminder"


def test_send_sms_request_template_key_required():
    with pytest.raises(ValidationError):
        SendSmsRequest(call_id=uuid.uuid4(), template_key="")


def test_sms_queued_response_shape():
    sid = uuid.uuid4()
    resp = SmsQueuedResponse(id=sid, status="pending")
    assert resp.id == sid
    assert resp.status == "pending"
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_tools_schemas.py -v
```
RED: `ImportError: cannot import name 'SendSmsRequest'`.

- [ ] Step 3: Implement

Append to `apps/api/src/usan_api/schemas/tools.py`:

```python
class SendSmsRequest(ToolCallRequest):
    # The LLM selects a template KEY only; it never authors free text (design §6.1).
    template_key: str = Field(min_length=1, max_length=64)


class SmsQueuedResponse(BaseModel):
    id: uuid.UUID
    status: str
```

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_tools_schemas.py -v && ruff check src/usan_api/schemas/tools.py && uv run mypy src/usan_api/schemas/tools.py
```

- [ ] Step 5: Commit

```bash
git add apps/api/src/usan_api/schemas/tools.py apps/api/tests/test_tools_schemas.py
git commit -m "feat(api): SendSmsRequest + SmsQueuedResponse schemas"
```

---

### Task D8: `tools.py` `POST /v1/tools/send_sms` (resolve config, find template, render, enqueue)

**Files:**
- Modify `apps/api/src/usan_api/routers/tools.py` (add imports + endpoint after `end_call`, before `log_transcript`).
- Test `apps/api/tests/test_tools.py` (append; uses real helpers per F5; profile publish/set_default per F4).

> **Depends-on (R10):** requires Part B migration 0011 + `SmsMessage` model + `repositories/sms_messages.py` (D6).

- [ ] Step 1: Write the failing test (uses `_create_elder`/`_enqueue`/`_auth`/`mock_dispatch` from test_tools.py; F4 publish/set_default; F5 helpers)

```python
# apps/api/tests/test_tools.py  (append)
import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories import agent_profiles as profiles_repo


def _publish_sms_profile(async_database_url, *, body="Hello {{first_name}} from USAN."):
    """Create a default-outbound profile whose draft enables send_sms with one template,
    publish it, set it default-outbound. Uses the REAL repo API (publish / set_default)."""
    async def _do():
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                profile = await profiles_repo.create_profile(
                    db, name=f"sms-{uuid.uuid4()}", description=None, actor_email="op@x.io"
                )
                draft = dict(profile.draft_config)
                tools = dict(draft.get("tools") or {})
                tools["enabled"] = list(tools.get("enabled") or [])
                if "send_sms" not in tools["enabled"]:
                    tools["enabled"].append("send_sms")
                tools["sms"] = {"templates": [{"key": "greet", "label": "Greet", "body": body}]}
                draft["tools"] = tools
                await profiles_repo.update_draft(
                    db, profile.id, config=draft, description=None, actor_email="op@x.io"
                )
                await profiles_repo.publish(db, profile.id, note=None, actor_email="op@x.io")
                await profiles_repo.set_default(db, profile.id, direction="outbound")
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(_do())


def test_send_sms_enqueues_pending_row(client, mock_dispatch, async_database_url):
    _publish_sms_profile(async_database_url)
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    # mark the call answered/in_progress is not required for enqueue; the endpoint
    # only needs the call + elder to exist (mirrors log_wellness, which works at QUEUED).
    r = client.post(
        "/v1/tools/send_sms",
        json={"call_id": call_id, "template_key": "greet"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending"
    uuid.UUID(body["id"])  # is a uuid


def test_send_sms_unknown_template_404(client, mock_dispatch, async_database_url):
    _publish_sms_profile(async_database_url)
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/send_sms",
        json={"call_id": call_id, "template_key": "nope"},
        headers=_auth(call_id),
    )
    assert r.status_code == 404


def test_send_sms_requires_token(client, mock_dispatch):
    elder_id = _create_elder(client)
    call_id = _enqueue(client, elder_id)
    r = client.post(
        "/v1/tools/send_sms", json={"call_id": call_id, "template_key": "greet"}
    )
    assert r.status_code == 401
```

> The default profile resolution walks `direction default` for an outbound call with no override; `_enqueue` creates an OUTBOUND call. The endpoint passes `direction="outbound"` to `resolve_agent_config`. When no profile resolves, it returns `DEFAULT_AGENT_CONFIG` whose `tools.sms is None` → the `send_sms`-not-configured 404 path is exercised by `test_send_sms_unknown_template_404` only after a profile IS published (so the 404 is genuinely "template key missing", not "no config").

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_tools.py -k "send_sms" -v
```
RED: `404` / `405` — `POST /v1/tools/send_sms` route does not exist yet (FastAPI returns 404 for the unknown path; the assertion `== 200` fails).

- [ ] Step 3: Implement

In `apps/api/src/usan_api/routers/tools.py`, add to imports:

```python
from usan_api import sms_render
from usan_api.repositories import agent_profiles as profiles_repo
from usan_api.repositories import sms_messages as sms_repo
from usan_api.schemas.tools import (
    ...,
    SendSmsRequest,
    SmsQueuedResponse,
    ...,
)
```

Add the endpoint after `end_call` (line 141) and before `log_transcript`:

```python
@router.post("/send_sms", response_model=SmsQueuedResponse)
@track_tool("send_sms")
async def send_sms(
    body: SendSmsRequest,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> SmsQueuedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    elder_id = _require_elder(call)
    elder = await elders_repo.get_elder(db, elder_id)
    if elder is None:
        raise HTTPException(status_code=409, detail="elder record not found")

    resolved = await profiles_repo.resolve_agent_config(
        db,
        profile_override=None,
        elder_profile_id=elder.agent_profile_id,
        direction=call.direction.value,
    )
    cfg = resolved.config if resolved is not None else DEFAULT_AGENT_CONFIG
    sms_cfg = cfg.tools.sms
    template = None
    if sms_cfg is not None:
        template = next((t for t in sms_cfg.templates if t.key == body.template_key), None)
    if template is None:
        # Either send_sms is not configured, or the key doesn't match a template.
        raise HTTPException(status_code=404, detail="sms template not found")

    rendered = sms_render.render_sms_body(template.body, call=call, elder=elder)
    row = await sms_repo.create_sms_message(
        db,
        call_id=call.id,
        elder_id=elder_id,
        to_number=elder.phone_e164,
        template_key=template.key,
        body=rendered,
    )
    await db.commit()
    # Does NOT send synchronously: flush_pending_sms delivers post-call (design §6.3).
    logger.bind(call_id=str(call.id)).info("Queued send_sms")
    return SmsQueuedResponse(id=row.id, status=row.status)
```

Add `DEFAULT_AGENT_CONFIG` to imports:

```python
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG
```

> `render_sms_body(template.body, call=call, elder=elder)` matches F8's required call shape (no `now=`). `resolve_agent_config` is given the elder's profile id as `elder_profile_id` and `direction=call.direction.value` so the precedence walk matches the runtime resolver.

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_tools.py -k "send_sms" -v && ruff check src/usan_api/routers/tools.py && uv run mypy src/usan_api/routers/tools.py
```

- [ ] Step 5: Commit

```bash
git add apps/api/src/usan_api/routers/tools.py apps/api/tests/test_tools.py
git commit -m "feat(api): POST /v1/tools/send_sms enqueues a pending SMS"
```

---

### Task D9: `sms_outbox.py` `flush_pending_sms` (own session, feature-flag, idempotent, `SMS_MESSAGES_TOTAL`)

**Files:**
- Create `apps/api/src/usan_api/sms_outbox.py`.
- Modify `apps/api/src/usan_api/observability/custom_metrics.py` (declare `SMS_MESSAGES_TOTAL` — this is the metric the SHARED CONTRACT assigns to messaging; `FOLLOWUP_FLAGS_TOTAL`/`CALLBACK_REQUESTS_TOTAL` are Part B/C). Part D declares only `SMS_MESSAGES_TOTAL`.
- Test `apps/api/tests/test_sms_outbox.py` (new).

> **Depends-on (R10):** requires Part B migration 0011 + `SmsMessage` model + D6 repo.

- [ ] Step 1: Write the failing test (idempotency + feature-flag-off + sent/failed)

```python
# apps/api/tests/test_sms_outbox.py  (new)
import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import sms_outbox, telnyx_messaging
from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import sms_messages as sms_repo


async def _seed_pending(url):
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    try:
        async with factory() as db:
            elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
            call = await calls_repo.create_call(
                db, elder_id=elder.id, direction=CallDirection.OUTBOUND, status=CallStatus.IN_PROGRESS
            )
            await sms_repo.create_sms_message(
                db, call_id=call.id, elder_id=elder.id,
                to_number=phone, template_key="t", body="hi",
            )
            await db.commit()
            return call.id
    finally:
        await engine.dispose()


async def _status_of(url, call_id):
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            rows = await sms_repo.list_messages(db, limit=100)
            return [r for r in rows if r.call_id == call_id]
    finally:
        await engine.dispose()


def test_flush_marks_failed_when_messaging_disabled(client, async_database_url, monkeypatch):
    # client fixture sets env; messaging is disabled by default (no TELNYX_MESSAGING_ENABLED).
    call_id = asyncio.run(_seed_pending(async_database_url))
    asyncio.run(sms_outbox.flush_pending_sms(call_id))
    rows = asyncio.run(_status_of(async_database_url, call_id))
    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert rows[0].error == {"reason": "messaging_disabled"}


def test_flush_sends_and_marks_sent(client, async_database_url, monkeypatch):
    monkeypatch.setenv("TELNYX_MESSAGING_ENABLED", "true")
    monkeypatch.setenv("TELNYX_MESSAGING_API_KEY", "KEY")
    monkeypatch.setenv("TELNYX_MESSAGING_PROFILE_ID", "mp1")
    monkeypatch.setenv("TELNYX_FROM_NUMBER", "+15551230000")
    from usan_api.settings import get_settings
    get_settings.cache_clear()

    async def _fake_send(settings, *, to_number, body):
        return "msg-xyz"

    monkeypatch.setattr(telnyx_messaging, "send_sms", _fake_send)

    call_id = asyncio.run(_seed_pending(async_database_url))
    asyncio.run(sms_outbox.flush_pending_sms(call_id))
    rows = asyncio.run(_status_of(async_database_url, call_id))
    assert rows[0].status == "sent"
    assert rows[0].telnyx_message_id == "msg-xyz"

    # Idempotent: a second flush re-sends nothing (row no longer pending).
    asyncio.run(sms_outbox.flush_pending_sms(call_id))
    rows2 = asyncio.run(_status_of(async_database_url, call_id))
    assert rows2[0].status == "sent"
    get_settings.cache_clear()


def test_flush_marks_failed_on_send_error(client, async_database_url, monkeypatch):
    monkeypatch.setenv("TELNYX_MESSAGING_ENABLED", "true")
    monkeypatch.setenv("TELNYX_MESSAGING_API_KEY", "KEY")
    monkeypatch.setenv("TELNYX_MESSAGING_PROFILE_ID", "mp1")
    monkeypatch.setenv("TELNYX_FROM_NUMBER", "+15551230000")
    from usan_api.settings import get_settings
    get_settings.cache_clear()

    async def _boom(settings, *, to_number, body):
        raise telnyx_messaging.TelnyxMessagingError("nope")

    monkeypatch.setattr(telnyx_messaging, "send_sms", _boom)
    call_id = asyncio.run(_seed_pending(async_database_url))
    asyncio.run(sms_outbox.flush_pending_sms(call_id))
    rows = asyncio.run(_status_of(async_database_url, call_id))
    assert rows[0].status == "failed"
    assert rows[0].error["reason"] == "send_failed"
    get_settings.cache_clear()
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_sms_outbox.py -v
```
RED: `ModuleNotFoundError: No module named 'usan_api.sms_outbox'` (and `AttributeError` on `SMS_MESSAGES_TOTAL`).

- [ ] Step 3: Implement

In `apps/api/src/usan_api/observability/custom_metrics.py`, after `TOOL_CALLS_TOTAL` (line 43) add:

```python
# status: sent|failed — the terminal outcome of a queued SMS row (incremented
# in flush_pending_sms AFTER the DB transition commits). PHI-free: no number/id.
SMS_MESSAGES_TOTAL = Counter(
    "usan_sms_messages",
    "SMS messages by terminal status.",
    labelnames=("status",),
)
```

Create `apps/api/src/usan_api/sms_outbox.py`:

```python
# apps/api/src/usan_api/sms_outbox.py  (new)
"""Post-call SMS flush (design §6.3). Runs via FastAPI BackgroundTasks AFTER the
response, so it OPENS ITS OWN session (the request session is already closed).

Idempotent: each row is claimed by a status-guarded pending->sent/failed
transition (sms_messages repo), so both completion paths (end_call + the
room_finished webhook) can fire for one call without re-sending. Gated on
TELNYX_MESSAGING_ENABLED; when off, rows are marked failed with a documented
reason (observable, not silent). The metric is incremented AFTER commit.
"""

import uuid

from loguru import logger

from usan_api import telnyx_messaging
from usan_api.db.session import get_session_factory
from usan_api.observability.custom_metrics import SMS_MESSAGES_TOTAL
from usan_api.repositories import sms_messages as sms_repo
from usan_api.settings import get_settings


async def flush_pending_sms(call_id: uuid.UUID) -> None:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as db:
        pending = await sms_repo.get_pending_for_call(db, call_id)
        if not pending:
            return

        if not settings.telnyx_messaging_enabled:
            for row in pending:
                await sms_repo.mark_failed(db, row.id, error={"reason": "messaging_disabled"})
            await db.commit()
            for _ in pending:
                SMS_MESSAGES_TOTAL.labels(status="failed").inc()
            logger.bind(call_id=str(call_id), n=len(pending)).info(
                "SMS flush skipped: messaging disabled"
            )
            return

        results: list[str] = []
        for row in pending:
            try:
                message_id = await telnyx_messaging.send_sms(
                    settings, to_number=row.to_number, body=row.body
                )
            except Exception as exc:  # noqa: BLE001 - any send failure marks the row failed
                await sms_repo.mark_failed(
                    db, row.id, error={"reason": "send_failed", "detail": str(exc)}
                )
                results.append("failed")
                continue
            await sms_repo.mark_sent(db, row.id, telnyx_message_id=message_id)
            results.append("sent")
        await db.commit()
        for outcome in results:
            SMS_MESSAGES_TOTAL.labels(status=outcome).inc()
        logger.bind(call_id=str(call_id), n=len(results)).info("SMS flush complete")
```

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_sms_outbox.py -v && ruff check src/usan_api/sms_outbox.py src/usan_api/observability/custom_metrics.py && uv run mypy src/usan_api/sms_outbox.py
```

- [ ] Step 5: Commit

```bash
git add apps/api/src/usan_api/sms_outbox.py apps/api/src/usan_api/observability/custom_metrics.py apps/api/tests/test_sms_outbox.py
git commit -m "feat(api): flush_pending_sms outbox (own session, feature-flag, idempotent)"
```

---

### Task D10: Wire `flush_pending_sms` via `BackgroundTasks` into `end_call` + `room_finished` — GENUINE RED gate (R8, F6)

**Files:**
- Modify `apps/api/src/usan_api/routers/tools.py` (`end_call`: add `BackgroundTasks` param + `add_task` when `updated is not None`).
- Modify `apps/api/src/usan_api/routers/webhooks.py` (room_finished branch: add `BackgroundTasks` param + `add_task` when the call completed).
- Test `apps/api/tests/test_sms_flush_wiring.py` (new).

> **Depends-on (R10):** requires D9 (`sms_outbox.flush_pending_sms`). The recorder is monkeypatched where the symbol is IMPORTED (F6): `usan_api.routers.tools.flush_pending_sms` for end_call; `usan_api.routers.webhooks.flush_pending_sms` for room_finished.

- [ ] Step 1: Write the failing test (IN_PROGRESS call + monkeypatched recorder + assert called once)

```python
# apps/api/tests/test_sms_flush_wiring.py  (new)
import asyncio
import base64
import hashlib
import json
import time
import uuid

import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo

_OP = {"Authorization": "Bearer " + "o" * 32}


def _service_token(call_id: str, secret: str = "s" * 32) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "call_id": call_id, "iat": now, "exp": now + 300},
        secret, algorithm="HS256",
    )


def _sign(body: str, key: str, secret: str) -> str:
    digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
    now = int(time.time())
    return jwt.encode(
        {"iss": key, "nbf": now - 5, "exp": now + 60, "sha256": digest},
        secret, algorithm="HS256",
    )


async def _seed_in_progress(url, room):
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    try:
        async with factory() as db:
            elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
            call = await calls_repo.create_call(
                db, elder_id=elder.id, direction=CallDirection.OUTBOUND,
                status=CallStatus.DIALING, livekit_room=room,
            )
            await calls_repo.mark_answered(db, call.id, sip_call_id="SCL")  # -> IN_PROGRESS
            await db.commit()
            return call.id
    finally:
        await engine.dispose()


def test_end_call_schedules_flush_once(client, async_database_url, monkeypatch):
    from usan_api.routers import tools as tools_router

    seen: list = []

    async def _recorder(call_id):
        seen.append(call_id)

    # Monkeypatch where it is IMPORTED (F6).
    monkeypatch.setattr(tools_router, "flush_pending_sms", _recorder)

    room = f"usan-outbound-{uuid.uuid4()}"
    call_id = asyncio.run(_seed_in_progress(async_database_url, room))
    r = client.post(
        "/v1/tools/end_call",
        json={"call_id": str(call_id), "reason": "check_in_complete"},
        headers={"Authorization": f"Bearer {_service_token(str(call_id))}"},
    )
    assert r.status_code == 200
    assert seen == [call_id]  # scheduled EXACTLY once (the IN_PROGRESS->COMPLETED transition)


def test_end_call_idempotent_replay_does_not_schedule_again(client, async_database_url, monkeypatch):
    from usan_api.routers import tools as tools_router

    seen: list = []

    async def _recorder(call_id):
        seen.append(call_id)

    monkeypatch.setattr(tools_router, "flush_pending_sms", _recorder)
    room = f"usan-outbound-{uuid.uuid4()}"
    call_id = asyncio.run(_seed_in_progress(async_database_url, room))
    hdr = {"Authorization": f"Bearer {_service_token(str(call_id))}"}
    client.post("/v1/tools/end_call", json={"call_id": str(call_id), "reason": "x"}, headers=hdr)
    client.post("/v1/tools/end_call", json={"call_id": str(call_id), "reason": "x"}, headers=hdr)
    # Second call is an idempotent no-op (updated is None) -> no extra schedule.
    assert seen == [call_id]


def test_room_finished_schedules_flush_once(client, async_database_url, monkeypatch):
    from usan_api.routers import webhooks as wh_router

    seen: list = []

    async def _recorder(call_id):
        seen.append(call_id)

    monkeypatch.setattr(wh_router, "flush_pending_sms", _recorder)
    room = f"usan-outbound-{uuid.uuid4()}"
    call_id = asyncio.run(_seed_in_progress(async_database_url, room))
    body = json.dumps(
        {"event": "room_finished", "room": {"name": room}, "id": "ev1", "createdAt": int(time.time())}
    )
    token = _sign(body, "key", "a" * 32)
    r = client.post(
        "/webhooks/livekit",
        content=body,
        headers={"Authorization": token, "Content-Type": "application/webhook+json"},
    )
    assert r.status_code == 200
    assert seen == [call_id]
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_sms_flush_wiring.py -v
```
RED mechanism (F6): before wiring, `flush_pending_sms` is **not imported** into `routers/tools.py` / `routers/webhooks.py`, so `monkeypatch.setattr(tools_router, "flush_pending_sms", ...)` raises `AttributeError: <module> has no attribute 'flush_pending_sms'`. Even if it were imported but never scheduled, `add_task` is never called → `seen == []` → `assert seen == [call_id]` fails. Deterministically RED.

- [ ] Step 3: Implement

In `apps/api/src/usan_api/routers/tools.py`, add imports:

```python
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from usan_api.sms_outbox import flush_pending_sms
```

Update `end_call` to accept `BackgroundTasks` and schedule the flush only on the real terminal transition:

```python
@router.post("/end_call", response_model=CallEndedResponse)
@track_tool("end_call")
async def end_call(
    body: EndCallRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_service_token),
) -> CallEndedResponse:
    call = await _authorize_call(body.call_id, claims, db)
    updated = await calls_repo.complete_call_if_in_progress(db, call.id, end_reason=body.reason)
    await db.commit()
    final = updated or call
    if updated is not None:
        CALLS_TOTAL.labels(direction=updated.direction.value, end_reason=updated.status.value).inc()
        # Deliver any queued SMS after the response (own session); idempotent so the
        # room_finished webhook firing too is safe (design §6.3).
        background_tasks.add_task(flush_pending_sms, call.id)
    logger.bind(call_id=str(call.id)).info("end_call requested")
    return CallEndedResponse(status=final.status.value)
```

In `apps/api/src/usan_api/routers/webhooks.py`, add imports:

```python
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from usan_api.sms_outbox import flush_pending_sms
```

Add `background_tasks: BackgroundTasks` to `livekit_webhook` and schedule the flush in the room_finished branch:

```python
@router.post("/livekit", status_code=status.HTTP_200_OK)
async def livekit_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, bool]:
    ...
    if event.event in _ROOM_END_EVENTS and event.room and event.room.name:
        call = await calls_repo.mark_completed_if_in_progress(db, event.room.name)
        if call is not None:
            await db.commit()
            background_tasks.add_task(flush_pending_sms, call.id)
            logger.bind(call_id=str(call.id), room=event.room.name).info(
                "Call completed via room_finished webhook"
            )
    ...
```

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_sms_flush_wiring.py tests/test_tools.py tests/test_webhooks.py -v && ruff check src/usan_api/routers/tools.py src/usan_api/routers/webhooks.py && uv run mypy src/usan_api/routers/tools.py src/usan_api/routers/webhooks.py
```

- [ ] Step 5: Commit

```bash
git add apps/api/src/usan_api/routers/tools.py apps/api/src/usan_api/routers/webhooks.py apps/api/tests/test_sms_flush_wiring.py
git commit -m "feat(api): wire flush_pending_sms via BackgroundTasks (end_call + room_finished)"
```

---

### Task D11: Agent `@function_tool send_sms` + `_do_send_sms` + `_TOOL_REGISTRY` insert (R5) + `api_client.send_sms` (R6)

**Files:**
- Modify `services/agent/src/usan_agent/check_in.py` (add `_do_send_sms` + `@function_tool send_sms`; insert ONE `_TOOL_REGISTRY` line before `"end_call": end_call,` per R5). Do NOT touch `_select_tools`.
- Modify `services/agent/src/usan_agent/api_client.py` (add `send_sms`).
- Test `services/agent/tests/test_check_in.py` + `services/agent/tests/test_api_client.py` (append).

- [ ] Step 1: Write the failing test

```python
# services/agent/tests/test_check_in.py  (append)
async def test_do_send_sms_calls_api_and_confirms(monkeypatch):
    from unittest.mock import AsyncMock
    spy = AsyncMock()
    monkeypatch.setattr(check_in.api_client, "send_sms", spy)
    result = await check_in._do_send_sms(_data(), template_key="med_reminder")
    spy.assert_awaited_once()
    assert spy.await_args.kwargs == {"template_key": "med_reminder"}
    assert spy.await_args.args[0] == "call-1"
    assert isinstance(result, str) and result  # a spoken confirmation


async def test_do_send_sms_handles_api_failure(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("api down")

    monkeypatch.setattr(check_in.api_client, "send_sms", _boom)
    result = await check_in._do_send_sms(_data(), template_key="x")
    assert isinstance(result, str) and result  # calm spoken fallback, never raises


def test_send_sms_registered_in_registry():
    assert check_in._TOOL_REGISTRY.get("send_sms") is check_in.send_sms
```

```python
# services/agent/tests/test_api_client.py  (append)
import pytest

from usan_agent import api_client


@pytest.mark.asyncio
async def test_send_sms_posts_template_key(monkeypatch):
    captured = {}

    async def _fake_post(tool, call_id, settings, payload):
        captured["tool"] = tool
        captured["call_id"] = call_id
        captured["payload"] = payload
        return {"id": "x", "status": "pending"}

    monkeypatch.setattr(api_client, "_post_tool", _fake_post)
    await api_client.send_sms("call-1", _settings_for_client(), template_key="med_reminder")
    assert captured["tool"] == "send_sms"
    assert captured["payload"] == {"template_key": "med_reminder"}


def _settings_for_client():
    from usan_agent.settings import Settings
    return Settings(
        LIVEKIT_API_KEY="k", LIVEKIT_API_SECRET="a" * 32, LIVEKIT_URL="ws://livekit:7880",
        CARTESIA_API_KEY="c", GCP_PROJECT="g", DEFAULT_CARTESIA_VOICE_ID="v",
        API_BASE_URL="http://api:8000", JWT_SIGNING_KEY="s" * 32,
    )
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd services/agent && uv run pytest tests/test_check_in.py -k "send_sms" tests/test_api_client.py -k "send_sms" -v
```
RED: `AttributeError: module 'usan_agent.check_in' has no attribute '_do_send_sms'` / `api_client has no attribute 'send_sms'`.

- [ ] Step 3: Implement

In `services/agent/src/usan_agent/check_in.py`, add `_do_send_sms` (after `_do_get_today_meds`, before `_do_end_call`):

```python
async def _do_send_sms(data: CheckInData, *, template_key: str) -> str:
    try:
        await api_client.send_sms(data.call_id, data.settings, template_key=template_key)
    except Exception:
        logger.bind(call_id=data.call_id).warning("send_sms tool failed")
        return "I wasn't able to send that text just now, but we can continue."
    return "I've sent that text message for you."
```

Add the `@function_tool` (after `get_today_meds`, before `end_call`):

```python
@function_tool
async def send_sms(ctx: RunContext[CheckInData], template_key: str) -> str:
    """Send the elder a pre-approved text message.

    Args:
        template_key: The id of the message template to send (choose from the
            available templates; you cannot write custom text).
    """
    return await _do_send_sms(ctx.userdata, template_key=template_key)
```

Insert ONE line into `_TOOL_REGISTRY` immediately BEFORE `"end_call": end_call,` (R5):

```python
_TOOL_REGISTRY: dict[str, Any] = {
    "log_wellness": log_wellness,
    "log_medication": log_medication,
    "get_today_meds": get_today_meds,
    "send_sms": send_sms,
    "end_call": end_call,
}
```

> Part D inserts only `"send_sms": send_sms,`. Parts B and C insert `flag_for_followup` / `schedule_callback` (also before `end_call`). Do NOT touch `_select_tools` — the R3 guard already drops `send_sms` when there is no template.

In `services/agent/src/usan_agent/api_client.py`, add (after `log_medication`):

```python
async def send_sms(call_id: str, settings: Settings, *, template_key: str) -> None:
    await _post_tool("send_sms", call_id, settings, {"template_key": template_key})
```

- [ ] Step 4: Run test, verify PASS

```bash
cd services/agent && uv run pytest tests/test_check_in.py tests/test_api_client.py -v && ruff check src/usan_agent/check_in.py src/usan_agent/api_client.py && uv run mypy src/usan_agent/check_in.py src/usan_agent/api_client.py
```

- [ ] Step 5: Commit

```bash
git add services/agent/src/usan_agent/check_in.py services/agent/src/usan_agent/api_client.py services/agent/tests/test_check_in.py services/agent/tests/test_api_client.py
git commit -m "feat(agent): send_sms function tool + registry entry + api_client.send_sms"
```

---

### Task D12: `schemas/admin_tools.py` `SmsMessageSummary` (omit body) + `routers/admin_tools.py` `GET /v1/admin/sms-messages` (ADD route)

**Files:**
- Modify `apps/api/src/usan_api/schemas/admin_tools.py` (ADD `SmsMessageSummary`; created in Part B per R7).
- Modify `apps/api/src/usan_api/routers/admin_tools.py` (ADD the route; router created + registered in Part B per R7 — Part D does NOT re-create or re-register).
- Test `apps/api/tests/test_admin_sms_messages_api.py` (new; cookie-jar `admin_session` fixture per the SHARED CONTRACT).

> **Depends-on (R10):** requires Part B migration 0011 + `SmsMessage` model + Part B's `routers/admin_tools.py` (existing & registered) + D6 repo.

- [ ] Step 1: Write the failing test

```python
# apps/api/tests/test_admin_sms_messages_api.py  (new)
import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.repositories import sms_messages as sms_repo


async def _seed(url, *, status):
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    try:
        async with factory() as db:
            elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
            call = await calls_repo.create_call(
                db, elder_id=elder.id, direction=CallDirection.OUTBOUND, status=CallStatus.IN_PROGRESS
            )
            row = await sms_repo.create_sms_message(
                db, call_id=call.id, elder_id=elder.id,
                to_number=phone, template_key="t", body="SECRET-BODY-TEXT",
            )
            if status != "pending":
                await sms_repo.mark_failed(db, row.id, error={"reason": "x"})
            await db.commit()
            return row.id
    finally:
        await engine.dispose()


def test_sms_messages_requires_admin_session(client):
    r = client.get("/v1/admin/sms-messages")
    assert r.status_code == 401


def test_sms_messages_lists_and_omits_body(client, admin_session, async_database_url):
    sms_id = asyncio.run(_seed(async_database_url, status="pending"))
    r = client.get("/v1/admin/sms-messages")
    assert r.status_code == 200
    items = r.json()
    assert any(i["id"] == str(sms_id) for i in items)
    for i in items:
        assert "body" not in i  # SmsMessageSummary OMITS the rendered body
        assert set(i.keys()) >= {"id", "call_id", "elder_id", "to_number", "template_key", "status"}


def test_sms_messages_status_filter(client, admin_session, async_database_url):
    asyncio.run(_seed(async_database_url, status="failed"))
    r = client.get("/v1/admin/sms-messages?status=failed")
    assert r.status_code == 200
    assert all(i["status"] == "failed" for i in r.json())
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_admin_sms_messages_api.py -v
```
RED: `404` on `GET /v1/admin/sms-messages` (route not added yet) → `assert r.status_code == 200` fails.

- [ ] Step 3: Implement

ADD to `apps/api/src/usan_api/schemas/admin_tools.py` (Part B created the file with `ConfigDict(from_attributes=True)` summaries):

```python
class SmsMessageSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    call_id: uuid.UUID
    elder_id: uuid.UUID
    to_number: str
    template_key: str
    status: str
    telnyx_message_id: str | None = None
    sent_at: datetime | None = None
    created_at: datetime
    # NOTE: the rendered `body` is intentionally OMITTED — it may carry the elder's
    # name / contextual content (design §9); summaries stay lean and lower-PHI.
```

> Ensure `uuid`, `datetime`, `BaseModel`, `ConfigDict` are imported (Part B already imports them for its summaries; add any missing).

ADD the route to `apps/api/src/usan_api/routers/admin_tools.py` (Part B created the router with `prefix="/v1/admin"`, `Depends(require_admin_session)`, and registered it in `main.py` — Part D only adds this handler):

```python
from usan_api.repositories import sms_messages as sms_repo
from usan_api.schemas.admin_tools import SmsMessageSummary


@router.get("/sms-messages", response_model=list[SmsMessageSummary])
async def list_sms_messages(
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[SmsMessageSummary]:
    rows = await sms_repo.list_messages(db, status=status, limit=limit)
    return [SmsMessageSummary.model_validate(r) for r in rows]
```

> The router-level `Depends(require_admin_session)` (added by Part B) already gates this route, so no per-route auth param is needed. Match Part B's `Query` import as it appears in the file. No `admin_audit.record` here — unlike follow-up-flags (which return PHI `reason`), the SMS summary omits `body`, so no PHI is returned and no audit entry is needed.

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_admin_sms_messages_api.py -v && ruff check src/usan_api/routers/admin_tools.py src/usan_api/schemas/admin_tools.py && uv run mypy src/usan_api/routers/admin_tools.py src/usan_api/schemas/admin_tools.py
```

- [ ] Step 5: Commit

```bash
git add apps/api/src/usan_api/routers/admin_tools.py apps/api/src/usan_api/schemas/admin_tools.py apps/api/tests/test_admin_sms_messages_api.py
git commit -m "feat(api): GET /v1/admin/sms-messages (summary omits body)"
```

---

### Task D13: admin-ui `agentConfigSchema` SMS sub-schema + PHI superRefine (D)

**Files:**
- Modify `apps/admin-ui/src/config/agentConfigSchema.ts` (add `smsTemplateSchema`; add `sms` to `toolsSchema`; add PHI name set).
- Test `apps/admin-ui/src/test/agentConfigSchema.test.ts` (append).

> Part A widened `TOOL_NAMES` to 7. Part D adds only the `sms` block + `smsTemplateSchema`.

- [ ] Step 1: Write the failing test

```typescript
// apps/admin-ui/src/test/agentConfigSchema.test.ts  (append inside the file)
import { smsTemplateSchema, toolsSchema } from "../config/agentConfigSchema";

describe("smsTemplateSchema", () => {
  it("accepts a non-PHI body", () => {
    const r = smsTemplateSchema.safeParse({
      key: "med_reminder",
      label: "Med reminder",
      body: "Hi {{first_name}}, reminder for {{current_date}}.",
    });
    expect(r.success).toBe(true);
  });

  it("rejects a non-slug key", () => {
    const r = smsTemplateSchema.safeParse({ key: "Bad Key", label: "x", body: "hi" });
    expect(r.success).toBe(false);
  });

  it.each(["last_check_in", "last_check_in_line", "last_mood", "last_pain", "today_meds"])(
    "hard-blocks PHI token %s in the body",
    (token) => {
      const r = smsTemplateSchema.safeParse({
        key: "k",
        label: "L",
        body: `Your status: {{${token}}}`,
      });
      expect(r.success).toBe(false);
    },
  );

  it("toolsSchema accepts an sms block with templates", () => {
    const r = toolsSchema.safeParse({
      enabled: ["log_wellness", "send_sms", "end_call"],
      sms: { templates: [{ key: "k", label: "L", body: "Hi {{first_name}}" }] },
    });
    expect(r.success).toBe(true);
  });

  it("toolsSchema sms is optional", () => {
    const r = toolsSchema.safeParse({ enabled: ["end_call"] });
    expect(r.success).toBe(true);
  });
});
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/admin-ui && npm test -- agentConfigSchema
```
RED: `smsTemplateSchema` is not exported (`undefined`) → `safeParse` throws / import fails.

- [ ] Step 3: Implement

In `apps/admin-ui/src/config/agentConfigSchema.ts`, add near the token regexes:

```typescript
// PHI built-in variable names (mirror apps/api PHI_BUILTIN_NAMES). An SMS template
// body referencing any of these hard-blocks (design §6.2) — stricter than greetings.
const PHI_TOKEN_NAMES = [
  "last_check_in",
  "last_check_in_line",
  "last_mood",
  "last_pain",
  "today_meds",
] as const;
// {{name}} token capture (mirrors DOUBLE_TOKEN_RE but captures the name).
const TOKEN_NAME_RE = /\{\{\s*([a-zA-Z0-9_]+)\s*\}\}/g;

export const smsTemplateSchema = z
  .object({
    key: z
      .string()
      .min(1)
      .max(64)
      .regex(/^[a-z0-9_]+$/, "key must be a lowercase slug (a-z, 0-9, _)"),
    label: z.string().min(1).max(120),
    body: z.string().min(1).max(480),
  })
  .superRefine((v, ctx) => {
    const phi = new Set<string>(PHI_TOKEN_NAMES);
    for (const m of v.body.matchAll(TOKEN_NAME_RE)) {
      const name = m[1];
      if (phi.has(name)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["body"],
          message: `SMS body must not reference protected health information ({{${name}}})`,
        });
      }
    }
  });
```

Extend `toolsSchema`:

```typescript
export const toolsSchema = z.object({
  enabled: z.array(z.enum(TOOL_NAMES)),
  sms: z.object({ templates: z.array(smsTemplateSchema) }).optional().nullable(),
});
```

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/admin-ui && npm test -- agentConfigSchema && npm run lint && npx tsc --noEmit
```

- [ ] Step 5: Commit

```bash
git add apps/admin-ui/src/config/agentConfigSchema.ts apps/admin-ui/src/test/agentConfigSchema.test.ts
git commit -m "feat(admin-ui): SMS template Zod schema + PHI hard-block superRefine"
```

---

### Task D14: admin-ui `ToolsSection` SMS templates editor + needs-templates hint + `fieldMeta` `tools.sms` (R9b)

**Files:**
- Modify `apps/admin-ui/src/features/editor/sections/ToolsSection.tsx` (add SMS templates editor + "enabled — needs templates" hint; catalog-driven base is Part A's).
- Modify `apps/admin-ui/src/config/fieldMeta.ts` (register `tools.sms` field meta).
- Test `apps/admin-ui/src/test/ToolsSection.test.tsx` (new) + `apps/admin-ui/src/test/fieldMeta.test.ts` (append).

> Part A already rewrote `ToolsSection` to be catalog-driven and removed the hardcoded `TOOL_HELP` (F2). Part D ADDS the SMS templates editor block + the hint, and registers `tools.sms` in `fieldMeta`.

- [ ] Step 1: Write the failing test

```typescript
// apps/admin-ui/src/test/fieldMeta.test.ts  (append)
describe("fieldMeta tools.sms", () => {
  it("registers tools.sms help mentioning templates and non-PHI", () => {
    const meta = fieldMeta["tools.sms"];
    expect(meta).toBeDefined();
    expect(meta!.label.toLowerCase()).toContain("sms");
    expect(meta!.help.toLowerCase()).toMatch(/template/);
    expect(meta!.help.toLowerCase()).toMatch(/non-phi|protected health|phi/);
  });
});
```

```typescript
// apps/admin-ui/src/test/ToolsSection.test.tsx  (new)
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { agentConfigSchema, type AgentConfigForm } from "../config/agentConfigSchema";
import { ToolsSection } from "../features/editor/sections/ToolsSection";

function Harness({ enabled }: { enabled: string[] }) {
  const form = useForm<AgentConfigForm>({
    resolver: zodResolver(agentConfigSchema),
    defaultValues: {
      // minimal valid-ish defaults; only tools matter for this section
      tools: { enabled, sms: null },
    } as unknown as AgentConfigForm,
  });
  return <ToolsSection form={form} />;
}

describe("ToolsSection SMS", () => {
  it("shows a needs-templates hint when send_sms is enabled but no templates exist", () => {
    render(<Harness enabled={["send_sms", "end_call"]} />);
    expect(screen.getByText(/needs templates/i)).toBeInTheDocument();
  });

  it("does not show the hint when send_sms is not enabled", () => {
    render(<Harness enabled={["log_wellness", "end_call"]} />);
    expect(screen.queryByText(/needs templates/i)).not.toBeInTheDocument();
  });
});
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/admin-ui && npm test -- fieldMeta ToolsSection
```
RED: `fieldMeta["tools.sms"]` is `undefined`; `ToolsSection` renders no "needs templates" hint.

- [ ] Step 3: Implement

Register `tools.sms` in `apps/admin-ui/src/config/fieldMeta.ts` (after the `tools.enabled` entry):

```typescript
  "tools.sms": {
    label: "SMS templates",
    help: "Operator-authored text templates the agent can send by key (it never writes free text). Bodies may use non-PHI variables only — a PHI variable is rejected (SMS is unencrypted). send_sms is offered to the agent only when at least one template exists.",
  },
```

In `apps/admin-ui/src/features/editor/sections/ToolsSection.tsx`, add the SMS editor + hint. The catalog-driven base list is Part A's; Part D appends an SMS block driven by `tools.sms` and `tools.enabled`:

```tsx
import { Controller, useFieldArray, type UseFormReturn } from "react-hook-form";
import type { AgentConfigForm } from "../../../config/agentConfigSchema";

// ...Part A's catalog-driven toggle list stays above...

function SmsTemplatesEditor({ form }: { form: UseFormReturn<AgentConfigForm> }) {
  const enabled = form.watch("tools.enabled") ?? [];
  const templates = form.watch("tools.sms.templates") ?? [];
  const { fields, append, remove } = useFieldArray({
    control: form.control,
    name: "tools.sms.templates",
  });
  const sendSmsEnabled = enabled.includes("send_sms");
  const needsTemplates = sendSmsEnabled && templates.length === 0;

  return (
    <div className="space-y-3 rounded-xl border border-slate-200 bg-white p-4 shadow-card">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-900">SMS templates</h3>
        <button
          type="button"
          className="rounded-lg border border-slate-300 px-2 py-1 text-xs"
          onClick={() => append({ key: "", label: "", body: "" })}
        >
          Add template
        </button>
      </div>
      {needsTemplates ? (
        <p className="text-xs font-medium text-amber-700">
          send_sms is enabled — needs templates: add at least one template or the agent
          cannot send any text.
        </p>
      ) : null}
      <ul className="space-y-3">
        {fields.map((f, i) => (
          <li key={f.id} className="space-y-2 rounded-lg border border-slate-100 p-3">
            <input
              className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
              placeholder="key (lowercase slug)"
              {...form.register(`tools.sms.templates.${i}.key` as const)}
            />
            <input
              className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
              placeholder="label"
              {...form.register(`tools.sms.templates.${i}.label` as const)}
            />
            <textarea
              className="w-full rounded border border-slate-300 px-2 py-1 text-sm"
              placeholder="body (non-PHI {{variables}} only)"
              {...form.register(`tools.sms.templates.${i}.body` as const)}
            />
            {form.formState.errors.tools?.sms?.templates?.[i]?.body ? (
              <p className="text-xs font-medium text-red-700">
                {form.formState.errors.tools.sms.templates[i]?.body?.message}
              </p>
            ) : null}
            <button
              type="button"
              className="text-xs text-red-700"
              onClick={() => remove(i)}
            >
              Remove
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
```

And render `<SmsTemplatesEditor form={form} />` at the end of `ToolsSection`'s returned markup (after the catalog toggle list and the existing error line).

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/admin-ui && npm test -- fieldMeta ToolsSection && npm run lint && npx tsc --noEmit
```

- [ ] Step 5: Commit

```bash
git add apps/admin-ui/src/features/editor/sections/ToolsSection.tsx apps/admin-ui/src/config/fieldMeta.ts apps/admin-ui/src/test/ToolsSection.test.tsx apps/admin-ui/src/test/fieldMeta.test.ts
git commit -m "feat(admin-ui): SMS templates editor + needs-templates hint + tools.sms field meta"
```

---

### Task D15: infra `.env.example` / `.env.prod.example` / `docker-compose.yml` + GENUINE yaml-load test (R8)

**Files:**
- Modify `infra/docker-compose.yml` (api service env, after line 195 `OPERATOR_API_KEY`).
- Modify `infra/.env.example` (after line 61 `TELNYX_CALLER_ID`).
- Modify `infra/.env.prod.example` (after line 44 `TELNYX_CALLER_ID`).
- Test `apps/api/tests/test_infra_messaging_env.py` (new; stdlib + PyYAML — PyYAML is in the api test deps).

- [ ] Step 1: Write the failing test (yaml.safe_load compose + .env.example contains keys)

```python
# apps/api/tests/test_infra_messaging_env.py  (new)
from pathlib import Path

import yaml

_REPO = Path(__file__).resolve().parents[3]
_KEYS = (
    "TELNYX_MESSAGING_API_KEY",
    "TELNYX_MESSAGING_PROFILE_ID",
    "TELNYX_FROM_NUMBER",
    "TELNYX_MESSAGING_ENABLED",
)


def test_compose_api_service_has_messaging_env():
    doc = yaml.safe_load((_REPO / "infra" / "docker-compose.yml").read_text())
    env = doc["services"]["api"]["environment"]
    # environment may be a dict or list of "K: v" / "K=v"; normalize to a key set.
    if isinstance(env, dict):
        keys = set(env.keys())
    else:
        keys = {str(item).split("=")[0].split(":")[0].strip() for item in env}
    for k in _KEYS:
        assert k in keys, f"{k} missing from api service environment"


def test_env_example_contains_messaging_keys():
    text = (_REPO / "infra" / ".env.example").read_text()
    for k in _KEYS:
        assert k in text, f"{k} missing from .env.example"


def test_env_prod_example_contains_messaging_keys():
    text = (_REPO / "infra" / ".env.prod.example").read_text()
    for k in _KEYS:
        assert k in text, f"{k} missing from .env.prod.example"
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd apps/api && uv run pytest tests/test_infra_messaging_env.py -v
```
RED: `AssertionError: TELNYX_MESSAGING_API_KEY missing from api service environment` (keys not yet added).

- [ ] Step 3: Implement

In `infra/docker-compose.yml`, in the `api` service `environment:` block, after `OPERATOR_API_KEY: ${OPERATOR_API_KEY}` (line 195):

```yaml
      # Telnyx Messaging (Phase 3 send_sms). Feature flag default FALSE: no SMS is
      # ever sent until a deploy sets TELNYX_MESSAGING_ENABLED=true AND the secret /
      # profile / from-number are populated. The 3 value fields are blank-able.
      TELNYX_MESSAGING_ENABLED: ${TELNYX_MESSAGING_ENABLED:-false}
      TELNYX_MESSAGING_API_KEY: ${TELNYX_MESSAGING_API_KEY:-}
      TELNYX_MESSAGING_PROFILE_ID: ${TELNYX_MESSAGING_PROFILE_ID:-}
      TELNYX_FROM_NUMBER: ${TELNYX_FROM_NUMBER:-}
      TELNYX_MESSAGING_API_URL: ${TELNYX_MESSAGING_API_URL:-https://api.telnyx.com/v2}
```

In `infra/.env.example`, after `TELNYX_CALLER_ID=` (line 61):

```
# --- Telnyx Messaging (Phase 3 send_sms; feature-flagged OFF by default) ---
# SMS is never sent unless TELNYX_MESSAGING_ENABLED=true AND the three values below
# are set. Bodies may use non-PHI variables only (enforced at save).
TELNYX_MESSAGING_ENABLED=false
TELNYX_MESSAGING_API_KEY=
TELNYX_MESSAGING_PROFILE_ID=
TELNYX_FROM_NUMBER=                   # E.164, e.g. +14155551234
```

In `infra/.env.prod.example`, after `TELNYX_CALLER_ID=` (line 44):

```
# --- Telnyx Messaging (Phase 3 send_sms; feature-flagged OFF by default) ---
# Enable only after the messaging secret is in Secret Manager + the VM .env is
# refreshed BEFORE the v* tag deploy (deploy mechanics: secret not auto-refreshed).
TELNYX_MESSAGING_ENABLED=false
TELNYX_MESSAGING_API_KEY=
TELNYX_MESSAGING_PROFILE_ID=
TELNYX_FROM_NUMBER=
```

- [ ] Step 4: Run test, verify PASS

```bash
cd apps/api && uv run pytest tests/test_infra_messaging_env.py -v
```

- [ ] Step 5: Commit

```bash
git add infra/docker-compose.yml infra/.env.example infra/.env.prod.example apps/api/tests/test_infra_messaging_env.py
git commit -m "feat(infra): Telnyx messaging env keys (compose + env examples, flag off)"
```

---

### Task D16: Grafana panel for `usan_sms_messages_total{status="failed"}` (R9c / F3)

**Files:**
- Modify `infra/grafana/dashboards/system.json` (ADD panel id 12 at `gridPos {x:0,y:37,w:12,h:8}`).
- Test `scripts/tests/test_system_dashboard.py` (append; uses the EXISTING contract — no new infra/tests tree, stdlib json only).

> **F3 note:** system.json panels allocated **B=id11/y29, D=id12/y37**. Part D adds id 12 only; Part B adds id 11 (`usan_followup_flags_total` by severity) at y29. Validate via the existing `scripts/tests` contract (`validate_dashboard` + `gridpos_overlaps`).

- [ ] Step 1: Write the failing test

```python
# scripts/tests/test_system_dashboard.py  (append)
def test_system_has_sms_failed_panel():
    doc = load_dashboard("system.json")
    panels = list(iter_panels(doc))
    sms = next((p for p in panels if p.get("id") == 12), None)
    assert sms is not None, "expected SMS-failed panel id 12 (Part D)"
    assert sms["gridPos"] == {"x": 0, "y": 37, "w": 12, "h": 8}
    exprs = " ".join(t.get("expr", "") for t in sms.get("targets", []))
    assert "usan_sms_messages_total" in exprs
    assert 'status="failed"' in exprs
    # still no overlap after the new panel
    assert gridpos_overlaps(panels) == []
    assert validate_dashboard(doc) == []
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine && python -m pytest scripts/tests/test_system_dashboard.py -k "sms_failed" -v
```
RED: `assert sms is not None` — no panel id 12 exists.

- [ ] Step 3: Implement

Append a panel to `infra/grafana/dashboards/system.json`'s top-level `panels` array (after the last existing panel; match the existing panel JSON shape — `datasource` uid `prometheus`, `type` timeseries, a `targets[].expr`). The panel object:

```json
{
  "id": 12,
  "title": "SMS delivery failures",
  "type": "timeseries",
  "datasource": { "type": "prometheus", "uid": "prometheus" },
  "gridPos": { "h": 8, "w": 12, "x": 0, "y": 37 },
  "fieldConfig": { "defaults": { "custom": {} }, "overrides": [] },
  "options": {},
  "targets": [
    {
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "expr": "sum(rate(usan_sms_messages_total{status=\"failed\"}[5m]))",
      "legendFormat": "failed/s",
      "refId": "A"
    }
  ]
}
```

> Match the exact `fieldConfig`/`options` keys used by the neighbouring timeseries panels in system.json so `validate_dashboard` passes (read panel id 5 "Tool calls" as the template). gridPos `y:37` sits directly below Part B's id-11 panel (`y:29,h:8` → ends at 37), so the two new panels do not overlap each other or the existing host panels (which end at y=29).
>
> **Alert (F3):** the urgent follow-up alert rule is **Part B's** deliverable. Part D ships only this panel. If `infra/grafana` codifies alert rules (`provisioning/alerting`), Part B follows that pattern; otherwise the PromQL alert expr is documented in the plan and the notification channel is a deploy step (spec §5.1). Part D's `usan_sms_messages_total{status="failed"}` is observable via this panel; no in-code alert is required for Part D.

- [ ] Step 4: Run test, verify PASS

```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine && python -m pytest scripts/tests/test_system_dashboard.py -v
```

- [ ] Step 5: Commit

```bash
git add infra/grafana/dashboards/system.json scripts/tests/test_system_dashboard.py
git commit -m "feat(infra): Grafana panel for usan_sms_messages_total failures (id 12)"
```

---

### Task D17: FINAL sync tests — agent `_TOOL_REGISTRY` == 7 names; api `TOOL_NAMES` == catalog names (R8, F9)

**Files:**
- Test `services/agent/tests/test_check_in.py` (append agent registry-sync test).
- Test `apps/api/tests/test_tool_catalog_sync.py` (new api sync test).

> **F9:** the agent registry-sync test is RED→GREEN only once D11 registers `send_sms`. This task runs AFTER D11 (it is the final task). The api `TOOL_NAMES` == catalog test goes green once Part A's `tool_catalog.py` exists and `agent_config.TOOL_NAMES` imports from it — already true when Part D runs.

- [ ] Step 1: Write the failing test

```python
# services/agent/tests/test_check_in.py  (append)
def test_tool_registry_has_exactly_the_seven_phase3_tools():
    from usan_agent import check_in
    assert set(check_in._TOOL_REGISTRY) == {
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "flag_for_followup",
        "schedule_callback",
        "send_sms",
        "end_call",
    }
```

```python
# apps/api/tests/test_tool_catalog_sync.py  (new)
from usan_api.schemas.agent_config import TOOL_NAMES
from usan_api.schemas.tool_catalog import TOOL_CATALOG, TOOL_NAMES as CATALOG_TOOL_NAMES


def test_tool_names_equals_catalog_names():
    catalog_names = {t.name for t in TOOL_CATALOG}
    assert TOOL_NAMES == catalog_names
    assert CATALOG_TOOL_NAMES == catalog_names


def test_catalog_has_exactly_seven_in_order():
    assert [t.name for t in TOOL_CATALOG] == [
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "flag_for_followup",
        "schedule_callback",
        "send_sms",
        "end_call",
    ]
```

- [ ] Step 2: Run test, verify it FAILS

```bash
cd services/agent && uv run pytest tests/test_check_in.py -k "seven_phase3" -v
cd ../../apps/api && uv run pytest tests/test_tool_catalog_sync.py -v
```
RED (agent): if run BEFORE D11, `send_sms` is absent → set inequality fails. After D11 it is GREEN. (Per F9 this task runs after D11, so the agent test is RED only relative to a pre-D11 registry — the executor must run D17 last.)
RED (api): green after Part A; included here to lock the API↔catalog invariant.

- [ ] Step 3: Implement

No new production code — D11 already registered `send_sms` (the last of the three registry entries B/C/D add). These are pure invariant assertions; the implementation that makes them pass is the cumulative A→D work.

- [ ] Step 4: Run test, verify PASS

```bash
cd services/agent && uv run pytest tests/test_check_in.py -v && uv run mypy .
cd ../../apps/api && uv run pytest tests/test_tool_catalog_sync.py -v && uv run mypy .
```

- [ ] Step 5: Commit

```bash
git add services/agent/tests/test_check_in.py apps/api/tests/test_tool_catalog_sync.py
git commit -m "test: catalog<->registry sync (agent 7-tool registry; api TOOL_NAMES==catalog)"
```

---

### Part D — final full-suite verification (run after D1–D17)

```bash
cd apps/api && uv run pytest -v && ruff check . && ruff format --check . && uv run mypy .
cd ../../services/agent && uv run pytest -v && ruff check . && uv run mypy .
cd ../../apps/admin-ui && npm test && npm run lint && npx tsc --noEmit
cd ../.. && python -m pytest scripts/tests/test_system_dashboard.py -v
```

**Key cross-references honored:** R1/R2/R3 untouched (D2 only adds `sms`; `_select_tools` guard via Part A's `getattr`); R5 single-line registry insert before `end_call`; R6 only `send_sms`'s 3 pieces; R7 admin_tools file/router additive (D12 ADDs route + summary, no re-register); R8 genuine RED gates (D10 monkeypatched recorder, D15 yaml-load, D17 registry sync); R9a sanitize in `sms_render`; R9b `tools.sms` fieldMeta; R9c Grafana id-12 panel; R10 DB-task depends-on Part B noted on D6/D8/D9/D10/D12; F3 panel allocation B=id11/y29, D=id12/y37; F4 real `publish`/`set_default`; F5 real `_create_elder`/`_enqueue`/`mark_answered` helpers; F6 monkeypatch at import site; F8 endpoint calls `render_sms_body(template.body, call=call, elder=elder)`; F9 D17 last.
