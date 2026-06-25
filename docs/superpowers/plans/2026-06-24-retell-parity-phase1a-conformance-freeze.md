# RetellAI Parity Phase 1a — Conformance Harness + Contract Freeze — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a pinned-oracle conformance test harness for the RetellAI-compat surface and resolve + freeze every PENDING-FREEZE shape on the existing core-calling endpoints, so the contract can no longer drift silently.

**Architecture:** Vendor RetellAI's real `openapi-final.yaml` (v3.0.0, 84 ops) into the repo as the checksum-pinned oracle; add a `tests/compat/oracle/` harness that validates our serialized request/response shapes against the oracle's JSON-Schema components (primary) and the official `retell-sdk` Pydantic models (secondary cross-check); then apply the 19 actionable PENDING-FREEZE resolutions and lock each behind a `@pytest.mark.frozen` test.

**Tech Stack:** Python 3.14, uv, FastAPI, Pydantic v2, pytest (sync `TestClient`), `jsonschema` validation, `retell-sdk==5.53.0`.

**Spec:** `docs/superpowers/specs/2026-06-24-retell-parity-phase1-core-calling-design.md` (§1, sub-PR 1a). **Roadmap:** `docs/superpowers/specs/2026-06-24-retell-full-parity-program-roadmap.md`.

## Global Constraints

- **Oracle pin:** `openapi-final.yaml` `info.version == 3.0.0`, openapi `3.0.3`, **84** path+method operations. SDK pin: `retell-sdk==5.53.0` (PyPI). Any oracle/SDK bump is a separate, human-reviewed PR.
- **Error envelope (unchanged):** every compat error is `{"status": <int code>, "message": <str>}` — never FastAPI's `{"detail": …}`. Verified shape: `body["status"] == 401`, `"detail" not in body`.
- **Auth/RLS (unchanged):** every compat route is Bearer-gated + org-RLS-scoped via `Depends(get_compat_db)`. Tests use the `compat_client` + `compat_headers` fixtures.
- **No behavior regressions:** existing `test_compat_*.py` must stay green. Run the full compat suite after every task.
- **Style:** ruff line-length 100, type hints required, `uv run mypy` clean (CI runs it). Commit format `type(scope): description`, scope `api`. Branch: `feat/retell-parity-phase1a` off `main`.
- **Test fixtures (in `apps/api/tests/conftest.py`):** `compat_client: TestClient`, `compat_headers: dict[str,str]` (a valid issued key), `compat_env`. Local helpers in `test_compat_calls.py`: `mock_dispatch` (monkeypatches `livekit_dispatch.dispatch_agent` + `dialer.schedule_dial`), `allow_quiet_hours`. Tests are **synchronous** `TestClient` calls.
- **Run tests:** `cd apps/api && uv run pytest -n0 tests/compat/ tests/test_compat_*.py -q` (serial while developing per CLAUDE.md; CI runs `-n auto`).

---

## File Structure

**New (harness):**
- `apps/api/tests/compat/__init__.py` — package marker.
- `apps/api/tests/compat/oracle/__init__.py` — package marker.
- `apps/api/tests/compat/oracle/openapi-final.yaml` — vendored pinned oracle (≈530 KB).
- `apps/api/tests/compat/oracle/SHA256SUMS` — checksum of the YAML.
- `apps/api/tests/compat/oracle/VERSION` — the string `3.0.0`.
- `apps/api/tests/compat/oracle_loader.py` — parse the YAML; expose `load_oracle()`, `oracle_operations()`, `component_schema(name)`.
- `apps/api/tests/compat/conformance.py` — `assert_conforms(payload, component_name)` (jsonschema against the vendored components) + `assert_sdk_roundtrip(payload, model_path)`.
- `apps/api/tests/compat/conftest.py` — shared `seeded_call` / `_create_agent` helpers.
- `apps/api/tests/compat/test_oracle_pin.py` — checksum + version + 84-op assertions.
- `apps/api/tests/compat/test_surface_coverage.py` — every oracle op served / 501 / known-gap.
- `apps/api/tests/compat/test_freeze_calls.py`, `test_freeze_agents.py`, `test_freeze_voices.py`, `test_freeze_batch.py`, `test_freeze_status_map.py`, `test_freeze_surface_roundtrip.py` — the `@pytest.mark.frozen` suites.

**Modified (resolutions):**
- `apps/api/pyproject.toml` — add `retell-sdk==5.53.0` + `jsonschema` to the test dependency group; register the `frozen` marker.
- `apps/api/src/usan_api/compat/schemas/calls.py` — override_agent_version union; sentiment/latency/role/collected pins; omit `transcript_with_tool_calls`; update-call `override_dynamic_variables` + `data_storage_setting` enum; custom_sip_headers str values.
- `apps/api/src/usan_api/compat/schemas/voices.py` — `provider`/`gender` enums.
- `apps/api/src/usan_api/compat/schemas/batch.py` — typed `call_time_window`.
- `apps/api/src/usan_api/compat/routers/agents.py` — `get-agent-versions` → full `AgentResponse[]`.
- `apps/api/src/usan_api/compat/routers/calls.py` — update-call `override_dynamic_variables`.
- `apps/api/src/usan_api/compat/call_serializer.py` — drop `transcript_with_tool_calls`.
- `apps/api/src/usan_api/compat/agent_bridge.py`, `batch_create.py`, `status_map.py`, `voice_map.py` — serialize-version / window map / docstring freezes.

---

## Task 1: Vendor the pinned oracle + checksum/shape guard

**Files:**
- Create: `apps/api/tests/compat/__init__.py`, `apps/api/tests/compat/oracle/__init__.py`, `apps/api/tests/compat/oracle/openapi-final.yaml`, `.../SHA256SUMS`, `.../VERSION`
- Create: `apps/api/tests/compat/oracle_loader.py`
- Test: `apps/api/tests/compat/test_oracle_pin.py`

**Interfaces:**
- Produces: `oracle_loader.load_oracle() -> dict`; `oracle_loader.oracle_operations() -> frozenset[tuple[str, str]]` (METHOD upper, path); `oracle_loader.ORACLE_PATH: Path`; `oracle_loader.ORACLE_DIR: Path`; `oracle_loader.component_schema(name: str) -> dict`.

- [ ] **Step 1: Vendor the YAML + record pins**

```bash
cd apps/api
mkdir -p tests/compat/oracle
touch tests/compat/__init__.py tests/compat/oracle/__init__.py
curl -fsSL https://docs.retellai.com/openapi-final.yaml -o tests/compat/oracle/openapi-final.yaml
printf '3.0.0\n' > tests/compat/oracle/VERSION
shasum -a 256 tests/compat/oracle/openapi-final.yaml | awk '{print $1"  openapi-final.yaml"}' > tests/compat/oracle/SHA256SUMS
```
If the fetch fails (docs gated), retrieve the same `openapi-final.yaml` from a `retell-sdk` release artifact or the Stainless spec URL in the SDK's `.stats.yml`; the checksum just pins whatever exact bytes you vendor.

- [ ] **Step 2: Write the loader**

```python
# apps/api/tests/compat/oracle_loader.py
from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

ORACLE_DIR = Path(__file__).parent / "oracle"
ORACLE_PATH = ORACLE_DIR / "openapi-final.yaml"
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


@cache
def load_oracle() -> dict:
    return yaml.safe_load(ORACLE_PATH.read_text())


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


def component_schema(name: str) -> dict:
    return load_oracle()["components"]["schemas"][name]
```

- [ ] **Step 3: Write the failing test**

```python
# apps/api/tests/compat/test_oracle_pin.py
from __future__ import annotations

import hashlib

from tests.compat.oracle_loader import ORACLE_DIR, ORACLE_PATH, load_oracle, oracle_operations


def test_oracle_checksum_matches_pin():
    recorded = (ORACLE_DIR / "SHA256SUMS").read_text().split()[0]
    actual = hashlib.sha256(ORACLE_PATH.read_bytes()).hexdigest()
    assert actual == recorded, "vendored oracle changed without a reviewed re-pin"


def test_oracle_version_and_shape():
    spec = load_oracle()
    assert spec["openapi"] == "3.0.3"
    assert spec["info"]["version"] == "3.0.0"
    assert (ORACLE_DIR / "VERSION").read_text().strip() == "3.0.0"


def test_oracle_has_84_operations():
    assert len(oracle_operations()) == 84
```

- [ ] **Step 4: Run it**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_oracle_pin.py -q`
Expected: PASS (3 tests). If `test_oracle_has_84_operations` fails, the vendored spec drifted from v3.0.0 — re-fetch the pinned version, do not edit the assertion.

- [ ] **Step 5: Commit**

```bash
git add apps/api/tests/compat
git commit -m "test(api): vendor pinned RetellAI oracle (openapi 3.0.0) + checksum guard"
```

---

## Task 2: Add `retell-sdk` + the conformance assertion helper

**Files:**
- Modify: `apps/api/pyproject.toml` (test dep group + `frozen` marker)
- Create: `apps/api/tests/compat/conformance.py`
- Test: `apps/api/tests/compat/test_conformance_selftest.py`

**Interfaces:**
- Produces: `conformance.assert_conforms(payload: dict, component: str) -> None`; `conformance.assert_sdk_roundtrip(payload: dict, model_path: str) -> None` (model_path like `"retell.types:CallResponse"`).

- [ ] **Step 1: Add deps + marker**

In `apps/api/pyproject.toml`, add to the existing dev/test dependency group (the group that already holds `pytest`):
```toml
    "retell-sdk==5.53.0",
    "jsonschema>=4.21",
```
And register the marker under `[tool.pytest.ini_options]`:
```toml
markers = [
    "frozen: contract-frozen conformance assertions against the pinned RetellAI oracle",
]
```
Then: `cd apps/api && uv sync`.

- [ ] **Step 2: Discover the exact SDK + oracle component names (one-off, record in a comment)**

Run: `cd apps/api && uv run python -c "import retell.types as t; print(sorted(n for n in dir(t) if n[0].isupper()))"`
Run: `cd apps/api && uv run python -c "from tests.compat.oracle_loader import load_oracle; print(sorted(load_oracle()['components']['schemas']))"`
Record the exact names for the Call / Agent / Voice / Concurrency response components + SDK models. Use the real names at every `assert_conforms(...)` / `assert_sdk_roundtrip(...)` call site below (the names used in later tasks — `CallResponse`, `AgentResponse`, `VoiceResponse`, `GetConcurrencyResponse` — are the expected values; substitute if the spec differs).

- [ ] **Step 3: Write the helper**

```python
# apps/api/tests/compat/conformance.py
from __future__ import annotations

import importlib
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.validators import RefResolver

from tests.compat.oracle_loader import component_schema, load_oracle


def assert_conforms(payload: dict[str, Any], component: str) -> None:
    """Validate ``payload`` against oracle ``components.schemas[component]`` with $ref resolution."""
    schema = component_schema(component)
    resolver = RefResolver.from_schema(load_oracle())  # resolves #/components/schemas/* refs
    Draft202012Validator(schema, resolver=resolver).validate(payload)


def assert_sdk_roundtrip(payload: dict[str, Any], model_path: str) -> None:
    """``model_path`` like 'retell.types:CallResponse' — assert the SDK model parses our payload."""
    module_name, _, attr = model_path.partition(":")
    model = getattr(importlib.import_module(module_name), attr)
    model.model_validate(payload)
```

- [ ] **Step 4: Write the self-test (RED then GREEN)**

```python
# apps/api/tests/compat/test_conformance_selftest.py
import pytest
from jsonschema.exceptions import ValidationError

from tests.compat.conformance import assert_conforms


def test_assert_conforms_rejects_bad_voice():
    # VoiceResponse requires voice_id/voice_name/provider — empty dict must fail.
    with pytest.raises(ValidationError):
        assert_conforms({}, "VoiceResponse")  # use the real component name from Step 2
```

- [ ] **Step 5: Run + commit**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_conformance_selftest.py -q` → PASS.
```bash
git add apps/api/pyproject.toml apps/api/uv.lock apps/api/tests/compat/conformance.py apps/api/tests/compat/test_conformance_selftest.py
git commit -m "test(api): add retell-sdk pin + oracle conformance assertion helper"
```

---

## Task 3: Surface-coverage test (every oracle op served / 501 / known-gap)

**Files:**
- Create: `apps/api/tests/compat/test_surface_coverage.py`

**Interfaces:**
- Consumes: `oracle_loader.oracle_operations()`; the compat app factory `usan_api.compat.app.build_compat_app`.
- Produces: `KNOWN_GAPS: frozenset[tuple[str,str]]` (the 6 Pri-1 endpoints + drifted-501 paths that **1b** resolves) — 1b shrinks this to empty.

- [ ] **Step 1: Write the test (with path normalization)**

```python
# apps/api/tests/compat/test_surface_coverage.py
from __future__ import annotations

import re

from usan_api.compat.app import build_compat_app
from usan_api.settings import get_settings
from tests.compat.oracle_loader import oracle_operations

_PARAM = re.compile(r"\{[^}]+\}")


def _norm(path: str) -> str:
    return _PARAM.sub("{}", path)  # {call_id} / {resource_id} → {} so naming differences don't matter


# Resolved in 1b (new endpoints + 501-router regeneration). (METHOD, normalized oracle path).
KNOWN_GAPS = frozenset({
    ("POST", "/v2/register-phone-call"),
    ("DELETE", "/v2/delete-call/{}"),
    ("PATCH", "/v2/update-live-call/{}"),
    ("DELETE", "/delete-agent-version/{}"),
    ("POST", "/v2/list-agents"),
    ("POST", "/publish-agent/{}"),
})


def _served() -> set[tuple[str, str]]:
    app = build_compat_app(get_settings())
    out: set[tuple[str, str]] = set()
    for route in app.routes:
        for method in getattr(route, "methods", set()) or set():
            out.add((method, _norm(route.path)))
    return out


def test_every_oracle_op_is_served_or_501_or_known_gap():
    served = _served()
    oracle = {(m, _norm(p)) for (m, p) in oracle_operations()}
    missing = sorted(op for op in oracle if op not in served and op not in KNOWN_GAPS)
    assert not missing, f"oracle ops neither served, 501-stubbed, nor known-gap: {missing}"
```

- [ ] **Step 2: Run**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_surface_coverage.py -q`
Expected: PASS. If it reports `missing` ops beyond `KNOWN_GAPS`, those are genuine 501-router drift the spec assigns to **1b** — add each to `KNOWN_GAPS` with a `# 1b: <reason>` comment (do not silently widen). The 1b plan removes them as the real routes/501s land.

- [ ] **Step 3: Commit**

```bash
git add apps/api/tests/compat/test_surface_coverage.py
git commit -m "test(api): assert compat surface accounts for all 84 oracle ops (known-gaps to 1b)"
```

---

## Task 4: Freeze — `override_agent_version` accepts int OR string

**Files:**
- Modify: `apps/api/src/usan_api/compat/schemas/calls.py:24-25`
- Create: `apps/api/tests/compat/conftest.py` (shared helpers), `apps/api/tests/compat/test_freeze_calls.py`

**Interfaces:**
- Produces: `tests/compat/conftest.py::seeded_call` fixture (`-> str` call_id) used by Tasks 6-8,14,15.

- [ ] **Step 1: Shared helpers + failing test**

```python
# apps/api/tests/compat/conftest.py
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from usan_api import dialer, livekit_dispatch, quiet_hours


@pytest.fixture
def mock_dispatch(monkeypatch):
    agent = AsyncMock()
    monkeypatch.setattr(livekit_dispatch, "dispatch_agent", agent)
    monkeypatch.setattr(dialer, "schedule_dial", lambda call_id, settings: None)
    return agent


@pytest.fixture
def allow_quiet_hours(monkeypatch):
    monkeypatch.setattr(quiet_hours, "next_allowed", lambda dt, tz, **k: dt)


def create_call(compat_client, compat_headers, **overrides):
    body = {"from_number": "+15551230000", "to_number": "+15557654321"}
    body.update(overrides)
    return compat_client.post("/v2/create-phone-call", json=body, headers=compat_headers)


@pytest.fixture
def seeded_call(compat_client, compat_headers, mock_dispatch, allow_quiet_hours) -> str:
    return create_call(compat_client, compat_headers).json()["call_id"]
```
```python
# apps/api/tests/compat/test_freeze_calls.py
from __future__ import annotations

import pytest

from tests.compat.conftest import create_call

pytestmark = pytest.mark.frozen


def test_override_agent_version_accepts_string_tag(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    r = create_call(compat_client, compat_headers, override_agent_version="latest")
    assert r.status_code == 201, r.text


def test_override_agent_version_accepts_int(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    r = create_call(compat_client, compat_headers, override_agent_version=3)
    assert r.status_code == 201, r.text
```

- [ ] **Step 2: Run → string case FAILS (422)**

Run: `cd apps/api && uv run pytest -n0 tests/compat/test_freeze_calls.py -q`

- [ ] **Step 3: Widen the type** in `schemas/calls.py`:
```python
    # FROZEN (oracle AgentVersionReference): int version OR string tag ("latest"/"prod").
    # Numeric selects that version; a string tag serves the current published version (MVP).
    override_agent_version: int | str | None = None
```

- [ ] **Step 4: Run → PASS (both).**

- [ ] **Step 5: Commit**

```bash
git add apps/api/src/usan_api/compat/schemas/calls.py apps/api/tests/compat/conftest.py apps/api/tests/compat/test_freeze_calls.py
git commit -m "fix(api): freeze override_agent_version as int|str (oracle AgentVersionReference)"
```

---

## Task 5: Freeze — `custom_sip_headers` str-valued echo

**Files:**
- Modify: `apps/api/src/usan_api/compat/schemas/calls.py` (`CreatePhoneCallRequest.custom_sip_headers`)
- Test: `apps/api/tests/compat/test_freeze_calls.py`

- [ ] **Step 1: Failing test**

```python
def test_custom_sip_headers_values_coerced_to_string(
    compat_client, compat_headers, mock_dispatch, allow_quiet_hours
):
    r = create_call(compat_client, compat_headers, custom_sip_headers={"X-Trace": 42})
    assert r.status_code == 201, r.text
    # Oracle additionalProperties:string → value is a string on the wire.
    assert r.json().get("metadata", {}) is not None  # sanity; create echoes don't surface headers
```

- [ ] **Step 2: Run** — today `dict[str, Any]` accepts the int, so add the type pin to enforce coercion. **Step 3:** in `schemas/calls.py`:
```python
    # FROZEN (oracle additionalProperties:string): accept + echo; values coerced to str.
    custom_sip_headers: dict[str, str] | None = None
```
Pydantic v2 coerces `{"X-Trace": 42}` → `{"X-Trace": "42"}`.

- [ ] **Step 4: Run → PASS.** **Step 5: Commit**
```bash
git add apps/api/src/usan_api/compat/schemas/calls.py apps/api/tests/compat/test_freeze_calls.py
git commit -m "fix(api): freeze custom_sip_headers as str-valued (oracle additionalProperties:string)"
```

---

## Task 6: Freeze — update-call `override_dynamic_variables` alias + `data_storage_setting` enum

**Files:**
- Modify: `apps/api/src/usan_api/compat/schemas/calls.py` (`UpdateCallRequest`), `apps/api/src/usan_api/compat/routers/calls.py:117-122`
- Test: `apps/api/tests/compat/test_freeze_calls.py`

**Interfaces:**
- Produces: `UpdateCallRequest.override_dynamic_variables` honored identically to `retell_llm_dynamic_variables` on the update op; `DataStorageSetting` enum.

- [ ] **Step 1: Failing tests**

```python
def test_update_call_accepts_override_dynamic_variables(compat_client, compat_headers, seeded_call):
    r = compat_client.patch(
        f"/v2/update-call/{seeded_call}",
        json={"override_dynamic_variables": {"first_name": "Bo"}},
        headers=compat_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["retell_llm_dynamic_variables"]["first_name"] == "Bo"


def test_update_call_rejects_bad_data_storage_setting(compat_client, compat_headers, seeded_call):
    r = compat_client.patch(
        f"/v2/update-call/{seeded_call}",
        json={"data_storage_setting": "bogus"},
        headers=compat_headers,
    )
    assert r.status_code == 422, r.text
```

- [ ] **Step 2: Run → both FAIL.**

- [ ] **Step 3: Implement.** In `schemas/calls.py`:
```python
from enum import Enum


class DataStorageSetting(str, Enum):
    everything = "everything"
    everything_except_pii = "everything_except_pii"
    basic_attributes_only = "basic_attributes_only"


class UpdateCallRequest(BaseModel):
    """PATCH /v2/update-call/{id}. ``override_dynamic_variables`` is the oracle field name on
    THIS op; ``data_storage_setting`` is enum-validated (no-op behavior); ``custom_attributes``
    is accepted/echoed."""

    metadata: dict[str, Any] | None = None
    retell_llm_dynamic_variables: dict[str, Any] | None = None
    override_dynamic_variables: dict[str, Any] | None = None
    data_storage_setting: DataStorageSetting | None = None
    custom_attributes: dict[str, Any] | None = None
```
In `routers/calls.py` `update_call`, replace the `if body.retell_llm_dynamic_variables is not None:` block with:
```python
    new_vars = body.override_dynamic_variables or body.retell_llm_dynamic_variables
    if new_vars is not None:
        dynamic_variables = new_vars
```

- [ ] **Step 4: Run → PASS.** **Step 5: Commit**
```bash
git add apps/api/src/usan_api/compat/schemas/calls.py apps/api/src/usan_api/compat/routers/calls.py apps/api/tests/compat/test_freeze_calls.py
git commit -m "fix(api): freeze update-call override_dynamic_variables alias + data_storage_setting enum"
```

---

## Task 7: Freeze — Call sub-object pins (sentiment vocab, transcript role, collected/latency null)

**Files:**
- Modify: `apps/api/src/usan_api/compat/schemas/calls.py` (comments on `CallAnalysis`, `TranscriptUtterance`, `CompatCall`)
- Test: `apps/api/tests/compat/test_freeze_calls.py`

- [ ] **Step 1: Conformance characterization test**

```python
from tests.compat.conformance import assert_conforms


@pytest.mark.xfail(reason="green after Task 8 omits transcript_with_tool_calls", strict=True)
def test_call_object_conforms_to_oracle(compat_client, compat_headers, seeded_call):
    body = compat_client.get(f"/v2/get-call/{seeded_call}", headers=compat_headers).json()
    assert_conforms(body, "CallResponse")  # real oracle component name from Task 2


def test_user_sentiment_default_is_null():
    from usan_api.compat.schemas.calls import CallAnalysis
    assert CallAnalysis().user_sentiment is None
```

- [ ] **Step 2: Run** — `test_call_object_conforms_to_oracle` is `xfail` (the only nonconformance is `transcript_with_tool_calls`, removed in Task 8); `test_user_sentiment_default_is_null` PASS.

- [ ] **Step 3: Freeze comments** in `schemas/calls.py`:
- `CallAnalysis.user_sentiment` → `# FROZEN: null now; non-null vocab is title-case Negative/Positive/Neutral/Unknown (oracle CallAnalysis).`
- `TranscriptUtterance` → `# FROZEN: role enum agent|user|transfer_target; words [] (oracle Utterance).`
- `CompatCall.collected_dynamic_variables` / `latency` → `# FROZEN: null pre-end; shape per oracle V3CallBase / CallLatency.`

- [ ] **Step 4: Run → unchanged (xfail strict still xfails).** **Step 5: Commit**
```bash
git add apps/api/src/usan_api/compat/schemas/calls.py apps/api/tests/compat/test_freeze_calls.py
git commit -m "test(api): pin Call sub-object shapes (sentiment/role/collected/latency) to oracle"
```

---

## Task 8: Freeze — omit `transcript_with_tool_calls` (genuine type mismatch)

**Files:**
- Modify: `apps/api/src/usan_api/compat/schemas/calls.py` (remove field), `apps/api/src/usan_api/compat/call_serializer.py` (stop setting it)
- Test: `apps/api/tests/compat/test_freeze_calls.py`

- [ ] **Step 1:** Remove the `@pytest.mark.xfail` decorator from `test_call_object_conforms_to_oracle` (Task 7) and add:
```python
def test_transcript_with_tool_calls_is_omitted(compat_client, compat_headers, seeded_call):
    body = compat_client.get(f"/v2/get-call/{seeded_call}", headers=compat_headers).json()
    assert "transcript_with_tool_calls" not in body
    assert "transcript" in body and "transcript_object" in body
```

- [ ] **Step 2: Run → both FAIL** (field present).

- [ ] **Step 3: Remove the field.** In `schemas/calls.py` `CompatCall`, delete `transcript_with_tool_calls: str | None = None`. In `call_serializer.py`, grep `transcript_with_tool_calls` and delete the keyword that sets it on `CompatCall`.

- [ ] **Step 4: Run → PASS** (both). Then `uv run pytest -n0 tests/test_compat_calls.py tests/compat/test_freeze_calls.py -q` → PASS.

- [ ] **Step 5: Commit**
```bash
git add apps/api/src/usan_api/compat/schemas/calls.py apps/api/src/usan_api/compat/call_serializer.py apps/api/tests/compat/test_freeze_calls.py
git commit -m "fix(api): omit transcript_with_tool_calls (oracle array vs our string) until array weave"
```

---

## Task 9: Freeze — status_map vocabulary

**Files:**
- Modify: `apps/api/src/usan_api/compat/status_map.py` (comments PENDING-FREEZE → FROZEN)
- Test: `apps/api/tests/compat/test_freeze_status_map.py`

- [ ] **Step 1: Lock test**
```python
# apps/api/tests/compat/test_freeze_status_map.py
import pytest

from usan_api.compat import status_map
from usan_api.db.base import CallStatus

pytestmark = pytest.mark.frozen


def test_busy_and_no_answer_map_to_ended():
    assert status_map.to_call_status(CallStatus.BUSY) == "ended"
    assert status_map.to_call_status(CallStatus.NO_ANSWER) == "ended"
    assert status_map.to_disconnection_reason(CallStatus.BUSY) == "dial_busy"
    assert status_map.to_disconnection_reason(CallStatus.NO_ANSWER) == "dial_no_answer"


def test_not_connected_is_never_emitted():
    assert "not_connected" not in {status_map.to_call_status(s) for s in CallStatus}


def test_failed_maps_to_dial_failed():
    assert status_map.to_disconnection_reason(CallStatus.FAILED) == "dial_failed"
```

- [ ] **Step 2: Run → PASS** (characterizes correct behavior). **Step 3:** flip the two `PENDING-FREEZE (oracle)` comments (lines ~14-15 and ~36) to `FROZEN (oracle V3CallBase enum / DisconnectionReason)`. **Step 4: Re-run → PASS.**

- [ ] **Step 5: Commit**
```bash
git add apps/api/src/usan_api/compat/status_map.py apps/api/tests/compat/test_freeze_status_map.py
git commit -m "test(api): freeze call status/disconnection mapping to oracle vocabulary"
```

---

## Task 10: Freeze — Voice `provider` + `gender` enums

**Files:**
- Modify: `apps/api/src/usan_api/compat/schemas/voices.py` (+ the catalog→VoiceResponse mapping that populates `gender`)
- Test: `apps/api/tests/compat/test_freeze_voices.py`

- [ ] **Step 1: Failing test**
```python
# apps/api/tests/compat/test_freeze_voices.py
import pytest

from tests.compat.conformance import assert_conforms

pytestmark = pytest.mark.frozen


def test_list_voices_conforms_to_oracle(compat_client, compat_headers):
    voices = compat_client.get("/list-voices", headers=compat_headers).json()
    assert voices, "expected a non-empty curated catalog"
    for v in voices:
        assert v["provider"] == "cartesia"
        assert v["gender"] in ("male", "female")
        assert_conforms(v, "VoiceResponse")  # real oracle component name
```

- [ ] **Step 2: Run → FAIL** (gender null and/or provider free string).

- [ ] **Step 3: Pin enums** in `schemas/voices.py`:
```python
from enum import Enum


class VoiceProvider(str, Enum):
    cartesia = "cartesia"


class VoiceGender(str, Enum):
    male = "male"
    female = "female"
```
Change `provider: str` → `provider: VoiceProvider` and `gender: str | None = None` → `gender: VoiceGender`. In the catalog→VoiceResponse mapping (grep `VoiceResponse(` under `compat/`), populate `gender` from `VOICE_CATALOG`. If the catalog lacks a gender field, add it to each `VOICE_CATALOG` entry (every curated voice has a known gender).

- [ ] **Step 4: Run → PASS.** Then `uv run pytest -n0 tests/test_compat_*.py -q` → no catalog regressions.

- [ ] **Step 5: Commit**
```bash
git add apps/api/src/usan_api/compat/schemas/voices.py apps/api/src/usan_api/schemas/voice_catalog.py apps/api/tests/compat/test_freeze_voices.py
git commit -m "fix(api): freeze voice provider=cartesia + non-null gender enum (oracle VoiceResponse)"
```

---

## Task 11: Freeze — `voice_id` alias contract

**Files:**
- Modify: `apps/api/src/usan_api/compat/voice_map.py` (docstring freeze)
- Test: `apps/api/tests/compat/test_freeze_voices.py`

- [ ] **Step 1: Lock tests**
```python
def test_voice_id_round_trips_retell_prefix():
    from usan_api.compat import voice_map
    from usan_api.schemas.voice_catalog import VOICE_CATALOG
    spec = VOICE_CATALOG[0]
    alias = voice_map.to_retell_voice_id(spec.cartesia_voice_id)
    assert alias.startswith("retell-")
    assert voice_map.resolve_voice_id(alias) == spec.cartesia_voice_id


def test_unhosted_voice_id_raises_422():
    from usan_api.compat import voice_map
    from usan_api.compat.errors import CompatError
    with pytest.raises(CompatError) as ei:
        voice_map.resolve_voice_id("11labs-Nonexistent")
    assert ei.value.status_code == 422
```

- [ ] **Step 2: Run → PASS.** **Step 3:** freeze the `voice_map.py` module-docstring `PENDING-FREEZE` note → `FROZEN (oracle VoiceResponse): retell-<Name> aliases + raw cartesia ids; 422 otherwise.` **Step 4: Re-run → PASS.**

- [ ] **Step 5: Commit**
```bash
git add apps/api/src/usan_api/compat/voice_map.py apps/api/tests/compat/test_freeze_voices.py
git commit -m "test(api): freeze voice_id alias contract (retell- prefix, 422 unhosted)"
```

---

## Task 12: Freeze — batch `call_time_window` typed echo + mapping

**Files:**
- Modify: `apps/api/src/usan_api/compat/schemas/batch.py`, `apps/api/src/usan_api/compat/batch_create.py`
- Test: `apps/api/tests/compat/test_freeze_batch.py`

**Interfaces:**
- Produces: `schemas/batch.CallTimeWindow{windows: list[CallTimeWindowSlot], timezone: str | None, day: list[int] | None}` replacing the opaque `dict`.

- [ ] **Step 1: Failing test**
```python
# apps/api/tests/compat/test_freeze_batch.py
import pytest

pytestmark = pytest.mark.frozen


def test_batch_call_time_window_typed_echo(compat_client, compat_headers, mock_dispatch, allow_quiet_hours):
    body = {
        "from_number": "+15551230000",
        "tasks": [{"to_number": "+15557654321"}],
        "call_time_window": {"windows": [{"start_hour": 9, "end_hour": 17}],
                             "timezone": "America/New_York", "day": [1, 2, 3, 4, 5]},
    }
    r = compat_client.post("/create-batch-call", json=body, headers=compat_headers)
    assert r.status_code == 201, r.text
    assert r.json()["call_time_window"]["timezone"] == "America/New_York"


def test_batch_call_time_window_rejects_garbage(compat_client, compat_headers, mock_dispatch, allow_quiet_hours):
    body = {"from_number": "+15551230000", "tasks": [{"to_number": "+15557654321"}],
            "call_time_window": {"windows": "not-a-list"}}
    r = compat_client.post("/create-batch-call", json=body, headers=compat_headers)
    assert r.status_code == 422, r.text
```
(`mock_dispatch`/`allow_quiet_hours` come from `tests/compat/conftest.py`.)

- [ ] **Step 2: Run → `rejects_garbage` FAILS** (opaque dict accepts anything).

- [ ] **Step 3: Type the model** (confirm field names via `component_schema("CallTimeWindow")`):
```python
class CallTimeWindowSlot(BaseModel):
    model_config = ConfigDict(extra="allow")
    start_hour: int = Field(ge=0, le=23)
    end_hour: int = Field(ge=0, le=23)


class CallTimeWindow(BaseModel):
    model_config = ConfigDict(extra="allow")
    windows: list[CallTimeWindowSlot] = Field(default_factory=list)
    timezone: str | None = None
    day: list[int] | None = None
```
Change both `call_time_window` fields (request + response) from `dict[str, Any] | None` to `CallTimeWindow | None = None`. In `batch_create.py`, map `windows[0]` + `day[]` onto the native batch window where expressible; where the native window cannot express it (multiple windows / cross-midnight), echo the typed value and leave the native window unset (documented partial map — never silently drop).

- [ ] **Step 4: Run → PASS.** Then `uv run pytest -n0 tests/test_compat_batches.py tests/compat/test_freeze_batch.py -q`.

- [ ] **Step 5: Commit**
```bash
git add apps/api/src/usan_api/compat/schemas/batch.py apps/api/src/usan_api/compat/batch_create.py apps/api/tests/compat/test_freeze_batch.py
git commit -m "fix(api): freeze batch call_time_window as typed CallTimeWindow (oracle) + map"
```

---

## Task 13: Fix+Freeze — `get-agent-versions` returns full `AgentResponse[]`

**Files:**
- Modify: `apps/api/src/usan_api/compat/routers/agents.py:138-154`, `apps/api/src/usan_api/compat/agent_bridge.py`
- Create: `apps/api/tests/compat/test_freeze_agents.py`

**Interfaces:**
- Produces: `agent_bridge.serialize_agent_version(agent_id: str, version_row) -> AgentResponse`.

- [ ] **Step 1: Failing test** (copy the exact happy-path create body from `tests/test_compat_agents.py`):
```python
# apps/api/tests/compat/test_freeze_agents.py
import pytest

from tests.compat.conformance import assert_conforms

pytestmark = pytest.mark.frozen


def _create_agent(compat_client, compat_headers) -> str:
    # Use the verbatim happy-path body from tests/test_compat_agents.py
    r = compat_client.post("/create-agent", json={...}, headers=compat_headers)
    assert r.status_code == 201, r.text
    return r.json()["agent_id"]


def test_get_agent_versions_returns_full_agent_objects(compat_client, compat_headers):
    agent_id = _create_agent(compat_client, compat_headers)
    versions = compat_client.get(f"/get-agent-versions/{agent_id}", headers=compat_headers).json()
    assert isinstance(versions, list) and versions
    for v in versions:
        assert v["agent_id"] == agent_id
        assert "voice_id" in v  # a full AgentResponse, not a 4-field dict
        assert_conforms(v, "AgentResponse")  # real oracle component name
```

- [ ] **Step 2: Run → FAIL** (returns 4-field dicts).

- [ ] **Step 3: Re-serialize.** In `agent_bridge.py` add:
```python
def serialize_agent_version(agent_id: str, version_row) -> AgentResponse:
    """Full AgentResponse for one historical version. Echoes current config with the row's
    version number overlaid (historical config snapshots are a known Phase-1 fidelity limit)."""
    base = serialize_agent(... current profile for agent_id ...)
    return base.model_copy(update={"version": version_row.version})
```
(Wire it to load the profile by `agent_id` as `get_agent_profile` does.) In `routers/agents.py` `get_agent_versions`, replace the dict-comprehension return with:
```python
    versions = await agent_bridge.list_agent_versions(db, agent_id)
    _audit(request, "get-agent-versions", agent_id)
    return [agent_bridge.serialize_agent_version(agent_id, v).model_dump() for v in versions]
```

- [ ] **Step 4: Run → PASS.** Then `uv run pytest -n0 tests/test_compat_agents.py tests/compat/test_freeze_agents.py -q`.

- [ ] **Step 5: Commit**
```bash
git add apps/api/src/usan_api/compat/routers/agents.py apps/api/src/usan_api/compat/agent_bridge.py apps/api/tests/compat/test_freeze_agents.py
git commit -m "fix(api): get-agent-versions returns full AgentResponse[] (oracle getAgentVersions)"
```

---

## Task 14: Freeze — get-agent ?version, retell-llm list-at-root, list-calls filter, publish authority, idempotency

**Files:**
- Modify: comment freezes in `routers/agents.py:62`, `routers/retell_llm.py` docstring, `routers/calls.py:131`, `agent_bridge.py:274` (publish authority — code change if it honors body version)
- Test: `apps/api/tests/compat/test_freeze_agents.py`, `test_freeze_calls.py`

- [ ] **Step 1: Lock tests**
```python
# test_freeze_agents.py
def test_get_agent_accepts_version_query_and_serves_current(compat_client, compat_headers):
    agent_id = _create_agent(compat_client, compat_headers)
    r = compat_client.get(f"/get-agent/{agent_id}?version=99", headers=compat_headers)
    assert r.status_code == 200, r.text

def test_list_retell_llms_is_bare_array_at_root(compat_client, compat_headers):
    r = compat_client.get("/list-retell-llms", headers=compat_headers)
    assert r.status_code == 200 and isinstance(r.json(), list)

def test_publish_returns_server_authoritative_version(compat_client, compat_headers):
    agent_id = _create_agent(compat_client, compat_headers)
    r = compat_client.post(f"/publish-agent-version/{agent_id}", json={"version": 999}, headers=compat_headers)
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["version"], int) and r.json()["version"] != 999
```
```python
# test_freeze_calls.py
def test_list_calls_filter_ignores_unknown_keys(compat_client, compat_headers, seeded_call):
    r = compat_client.post("/v3/list-calls", json={"filter_criteria": {"unknown": "x"}, "limit": 5}, headers=compat_headers)
    assert r.status_code == 200, r.text

def test_duplicate_create_is_idempotent(compat_client, compat_headers, mock_dispatch, allow_quiet_hours):
    a = create_call(compat_client, compat_headers).json()["call_id"]
    b = create_call(compat_client, compat_headers).json()["call_id"]
    assert a == b  # sha256 dedupe (call_create.py:50/78) — same params → same call
```

- [ ] **Step 2: Run.** Most PASS. If `test_publish_returns_server_authoritative_version` fails, change `agent_bridge.publish_agent_version` to auto-assign the version and treat `body.version` as advisory.

- [ ] **Step 3: Freeze comments** — flip `PENDING-FREEZE` → `FROZEN (oracle …)` at `agents.py:62`, the `retell_llm.py` list-prefix docstring, `calls.py:131`, `call_create.py:50` + `:78`, `agent_bridge.py:274`. Apply the publish-authority code change if needed.

- [ ] **Step 4: Run → PASS** (all + existing suites).

- [ ] **Step 5: Commit**
```bash
git add apps/api/src/usan_api/compat apps/api/tests/compat
git commit -m "test(api): freeze get-agent ?version, retell-llm root list, list-calls filter, publish authority, idempotency"
```

---

## Task 15: Full-surface frozen round-trip + final gate + PR

**Files:**
- Create: `apps/api/tests/compat/test_freeze_surface_roundtrip.py`

- [ ] **Step 1: Cross-endpoint round-trip**
```python
# apps/api/tests/compat/test_freeze_surface_roundtrip.py
import pytest

from tests.compat.conformance import assert_conforms, assert_sdk_roundtrip

pytestmark = pytest.mark.frozen


def test_concurrency_conforms(compat_client, compat_headers):
    body = compat_client.get("/get-concurrency", headers=compat_headers).json()
    assert_conforms(body, "GetConcurrencyResponse")  # real oracle component name


def test_call_object_sdk_roundtrip(compat_client, compat_headers, seeded_call):
    body = compat_client.get(f"/v2/get-call/{seeded_call}", headers=compat_headers).json()
    assert_sdk_roundtrip(body, "retell.types:CallResponse")  # real SDK model from Task 2
```

- [ ] **Step 2: Run the frozen suite + whole surface**

Run: `cd apps/api && uv run pytest -n0 -m frozen -q` → PASS.
Run: `cd apps/api && uv run pytest -n0 tests/compat/ tests/test_compat_*.py -q` → PASS.

- [ ] **Step 3: Confirm CI collection** — `tests/compat/` is already on the pytest path (`apps/api/tests/`); the "Lint Python" job runs `-n auto` over `tests/`. Verify: `uv run pytest -q tests/compat/ --collect-only | tail -1` shows all freeze tests collected.

- [ ] **Step 4: Full gate**

Run: `cd apps/api && uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: all green.

- [ ] **Step 5: Commit + PR**
```bash
git add apps/api/tests/compat
git commit -m "test(api): full-surface frozen conformance round-trip (Phase 1a complete)"
git push -u origin feat/retell-parity-phase1a
gh pr create --title "feat(api): RetellAI parity Phase 1a — conformance harness + contract freeze" \
  --body "Implements docs/superpowers/specs/2026-06-24-retell-parity-phase1-core-calling-design.md §1a. Vendors the pinned oracle (openapi 3.0.0, 84 ops) + retell-sdk 5.53.0, adds the @pytest.mark.frozen conformance harness, and resolves all 19 actionable PENDING-FREEZE markers. KNOWN_GAPS (6 Pri-1 endpoints + 501 drift) deferred to 1b."
```

---

## Self-Review

**Spec coverage (§1a):** harness vendoring/SDK-pin/validators → Tasks 1-3,15; FROZEN gate → all freeze tasks via `@pytest.mark.frozen`; the 19 actionable PENDING-FREEZE → Task 4 (override union), 5 (sip headers), 6 (update-call alias+enum), 7 (sentiment/role/collected/latency), 8 (transcript_with_tool_calls), 9 (status_map ×2), 10 (voice provider/gender), 11 (voice_id alias), 12 (batch window), 13 (publish authority lands in 14), 14 (get-agent ?version, retell-llm list prefix, filter breadth, publish authority, idempotency dedupe + contact-metadata keys); existing-endpoint shape fixes → 4,5,6,8,10,13. The 2 docstring markers become test-frozen via the conformance suite. All 21 markers covered.

**Placeholder scan:** the only `{...}` is in Task 13's `_create_agent` body, deliberately delegated to "copy the verbatim happy-path body from `tests/test_compat_agents.py`" (the canonical example) rather than guessing the agent schema — an explicit grounding instruction, not a vague placeholder. The "real component/SDK model name" notes name the expected identifier + the exact discovery command (Task 2). No "TBD/add error handling/similar to".

**Type consistency:** `assert_conforms(payload, component)` / `assert_sdk_roundtrip(payload, model_path)` stable across Tasks 2,7,10,13,15; `oracle_operations()`/`component_schema()`/`load_oracle()` stable; `create_call`/`seeded_call`/`mock_dispatch`/`allow_quiet_hours` defined once in `tests/compat/conftest.py` (Task 4) and reused; `serialize_agent_version` defined + used in Task 13; `DataStorageSetting`/`VoiceProvider`/`VoiceGender`/`CallTimeWindow`/`CallTimeWindowSlot` each defined once.
