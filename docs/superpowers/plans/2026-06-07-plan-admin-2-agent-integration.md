# Admin UI — Phase 2: Agent Integration (Resolve & Apply Config at Call Time) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the LiveKit agent fetch the *resolved, published* agent configuration from `apps/api` at the start of every call and build its STT/LLM/TTS/VAD/turn-detection/endpointing pipeline, prompts, tools, and voicemail behavior from that config — degrading safely to the current hardcoded defaults whenever resolution or the network fails.

**Architecture:** `apps/api` exposes one read-only endpoint, `GET /v1/runtime/agent-config?direction=&call_id=`, guarded by the existing **worker JWT** (`require_worker_token`). It resolves a profile by precedence — `call.profile_override` → `elder.agent_profile_id` → the direction default — takes that profile's published version snapshot, validates it into `AgentConfig`, and returns it (always 200; falls back to the server's `DEFAULT_AGENT_CONFIG` when nothing resolves). The agent owns a **parallel, lean copy** of the config model (`apps/api` and `services/agent` must not import each other), fetches once per call right after `ctx.connect()`, and threads the typed `cfg` through every builder. Any failure → the agent's local `DEFAULT_AGENT_CONFIG`, which reproduces today's constants, so the call always proceeds.

**Tech Stack:** Python 3.14 (`apps/api`, uv) + Python 3.12 (`services/agent`, uv); FastAPI; SQLAlchemy 2.0 async; Pydantic v2; LiveKit Agents 1.5.14 (`silero`, `cartesia`, `google`, `turn-detector` plugins); pytest + Postgres testcontainer; ruff + mypy in CI.

---

## Key Decisions (read before starting)

1. **Single endpoint, worker-token auth.** `require_service_token` requires a `call_id` claim; inbound has no `call_id` at fetch time, so we use `require_worker_token` (signature + `exp` only) — the same token the agent already mints for `/v1/calls/inbound`. The resolved config is profile-global (prompts/voice/model/timing), **not** per-elder PHI, so worker scope is sufficient.
2. **Always 200 with a usable config.** When nothing resolves (no default set, unpublished, or a stored config that fails validation), return `DEFAULT_AGENT_CONFIG` with `source:"default"`. The agent's *local* default is only for when the HTTP fetch itself fails. Both defaults reproduce today's constants, so they match.
3. **Resolution precedence walks and falls through.** Order: `profile_override`, then `elder.agent_profile_id`, then direction default. For each candidate, the profile must be **ACTIVE** and have a **published** version whose stored JSON **validates**; otherwise fall through to the next tier. Archived/deleted/unpublished/invalid never raise — they degrade.
4. **No cross-package import.** `services/agent/src/usan_agent/agent_config.py` is a *parallel copy* of the model tree + `DEFAULT_AGENT_CONFIG`, leaner than the API's (no admin-write validators). It becomes the agent's **single source of default truth**: `pipeline.py`/`check_in.py` re-export their constants from it, eliminating in-agent drift. A guard test asserts the copy equals the current constants at the moment of creation.
5. **`None` ≠ omitted for plugin kwargs.** Every optional speech/voice/LLM knob is `NotGivenOr` in the installed plugins. Build kwargs **conditionally** (a dict, add the key only when the field is non-`None`) so unset knobs preserve today's plugin defaults exactly. Verified signatures (livekit-agents 1.5.14):
   - `silero.VAD.load(*, min_silence_duration=0.55, activation_threshold=0.5, ...)`
   - `cartesia.STT(*, model='ink-whisper', language='en', api_key=..., ...)`
   - `cartesia.TTS(*, model='sonic-3', language='en', voice=..., speed=None, api_key=..., ...)`
   - `google.LLM(*, model, vertexai, project, location, temperature=NOT_GIVEN, ...)`
   - `AgentSession(*, stt, vad, llm, tts, turn_detection, userdata, min_endpointing_delay=NOT_GIVEN, max_endpointing_delay=NOT_GIVEN, min_interruption_duration=NOT_GIVEN, min_interruption_words=NOT_GIVEN, ...)`
   - `from livekit.plugins.turn_detector.multilingual import MultilingualModel` (English at `...turn_detector.english`).
6. **`end_call` is never disabled.** It drives report→goodbye→`delete_room`→`shutdown`. `_select_tools` force-includes it even if config omits it.
7. **Voicemail matcher is per-call and literal.** Inject a compiled `re.Pattern` into `VoicemailWatcher`. Empty `trigger_phrases` → built-in `_PATTERN`; non-empty → `re.escape` each phrase, OR-join, `re.IGNORECASE` (literal, never raw regex — a false positive hangs up on a live elder). Never mutate the module-global pattern.
8. **Voicemail is outbound-only** (spec §7); inbound paths get no watcher. `voicemail_detection` config affects only the outbound detection window.
9. **Backward-compatible signatures during the rollout.** Every new builder param (`cfg`) is **optional** (`cfg: AgentConfig | None = None` → `DEFAULT_AGENT_CONFIG`), so each task leaves the suite green. The worker-level test stubs are updated only in Task 9, where `cfg` is actually threaded into `worker.py`.
10. **PHI/secrets discipline.** No prompt text, elder data, or config bodies in logs (log profile id + version only). The agent's fetch uses `settings.api_base_url` (never a hardcoded URL) so the plaintext-http fail-closed rule holds.

---

## File Structure

**`apps/api` (created):**
- `src/usan_api/routers/runtime.py` — the `GET /v1/runtime/agent-config` endpoint.
- `tests/test_runtime.py` — endpoint tests (auth, default, resolved, invalid direction).
- `tests/test_agent_config_resolve.py` — repo-level precedence tests.

**`apps/api` (modified):**
- `src/usan_api/schemas/agent_config.py` — add `ResolvedAgentConfig` response model.
- `src/usan_api/repositories/agent_profiles.py` — add `get_default_profile`, `get_published_config`, `_resolved_from_profile`, `resolve_agent_config`.
- `src/usan_api/main.py` — register the runtime router.

**`services/agent` (created):**
- `src/usan_agent/agent_config.py` — parallel config model tree + `DEFAULT_AGENT_CONFIG`.
- `tests/test_agent_config.py` — model parse/leniency tests.
- `tests/test_agent_config_defaults.py` — drift guard (default == current constants).

**`services/agent` (modified):**
- `src/usan_agent/api_client.py` — add `fetch_agent_config`.
- `src/usan_agent/pipeline.py` — `build_session(cfg)`, `build_agent(cfg)`, `greet(cfg)`, `say_recording_disclosure(cfg)`, `_build_turn_detection`; constants re-exported from `agent_config`.
- `src/usan_agent/check_in.py` — `_TOOL_REGISTRY`, `_select_tools`, `build_check_in_agent(cfg)`, `build_inbound_agent(cfg, dynamic_vars)`, goodbye via `CheckInData`, templated `_inbound_instructions`.
- `src/usan_agent/voicemail.py` — `build_matcher`, `VoicemailWatcher(matcher=...)`.
- `src/usan_agent/voicemail_action.py` — `leave_voicemail(..., voicemail_message)`.
- `src/usan_agent/worker.py` — fetch `cfg` in `entrypoint`, thread through all paths, answer-timeout + max-duration watchdog from `cfg`.
- Test updates in `tests/test_pipeline.py`, `tests/test_check_in.py`, `tests/test_voicemail.py`, `tests/test_voicemail_action.py`, `tests/test_worker.py`, `tests/test_recording_consent.py`, `tests/test_api_client.py`.

---

## Task 1: Agent-side config model + default (single source of agent defaults)

**Files:**
- Create: `services/agent/src/usan_agent/agent_config.py`
- Test: `services/agent/tests/test_agent_config.py`
- Test: `services/agent/tests/test_agent_config_defaults.py`

- [ ] **Step 1: Write the failing model tests**

Create `services/agent/tests/test_agent_config.py`:

```python
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig


def test_default_is_complete_and_branded():
    cfg = DEFAULT_AGENT_CONFIG
    assert "USAN" in cfg.prompts.greeting
    assert cfg.prompts.system_prompt.startswith("You are a warm")
    assert "log_wellness" in cfg.prompts.checkin_flow_instructions
    assert cfg.llm.model.startswith("gemini")
    assert cfg.stt.model == "ink-whisper"
    assert cfg.timing.answer_timeout_s == 50.0
    assert cfg.timing.max_call_duration_s == 1800
    assert cfg.tools.enabled == ["log_wellness", "log_medication", "get_today_meds", "end_call"]
    assert cfg.voicemail_detection.window_s == 3.0
    assert cfg.voicemail_detection.trigger_phrases == []


def test_parse_minimal_prompts_only_document():
    # The server always sends a full document, but parsing must succeed from just the
    # required prompts block, defaulting every optional sub-config.
    doc = {
        "prompts": {
            "system_prompt": "sys",
            "greeting": "hi",
            "recording_disclosure": "rec",
            "voicemail_message": "vm",
            "checkin_flow_instructions": "flow",
            "goodbye_message": "bye",
            "inbound_opening": "open",
            "inbound_personalization_template": "hello {elder_name} {last_check_in_line}",
        }
    }
    cfg = AgentConfig.model_validate(doc)
    assert cfg.voice.cartesia_voice_id is None
    assert cfg.llm.model.startswith("gemini")  # default applied
    assert cfg.speech_advanced.turn_detection is None


def test_parse_ignores_unknown_fields():
    doc = DEFAULT_AGENT_CONFIG.model_dump()
    doc["prompts"]["a_future_field"] = "ignored"
    doc["a_future_top_level"] = {"x": 1}
    cfg = AgentConfig.model_validate(doc)  # must not raise
    assert cfg.prompts.greeting == DEFAULT_AGENT_CONFIG.prompts.greeting


def test_roundtrip_dump_then_validate():
    cfg = AgentConfig.model_validate(DEFAULT_AGENT_CONFIG.model_dump())
    assert cfg == DEFAULT_AGENT_CONFIG
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/agent && uv run pytest tests/test_agent_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'usan_agent.agent_config'`

- [ ] **Step 3: Create the model**

Create `services/agent/src/usan_agent/agent_config.py`:

```python
"""Agent-side copy of the admin-editable configuration document.

`apps/api` and `services/agent` must not import each other (CLAUDE.md), so this is
a deliberate parallel copy of `apps/api/.../schemas/agent_config.py`. It is leaner:
the admin-write validators (brace rejection, slot allow-list, tool-name checks) live
on the API write path; the agent only PARSES a server-validated document and reads
typed fields. Extra fields are ignored (pydantic v2 default), so a future API field
never breaks the agent. `DEFAULT_AGENT_CONFIG` reproduces the agent's current
constants and is the single source of default truth: pipeline.py / check_in.py
re-export their constants from here. Keep field names/defaults in sync with the API
copy; the response JSON's `config` block is parsed straight into AgentConfig.
"""

from typing import Literal

from pydantic import BaseModel, Field


class PromptsConfig(BaseModel):
    system_prompt: str
    greeting: str
    recording_disclosure: str
    voicemail_message: str
    checkin_flow_instructions: str
    goodbye_message: str
    inbound_opening: str
    inbound_personalization_template: str


class VoiceConfig(BaseModel):
    cartesia_voice_id: str | None = None
    tts_model: str | None = None
    speed: float | None = None
    language: str | None = None


class LLMConfig(BaseModel):
    model: str = "gemini-3.1-flash-lite"
    temperature: float | None = None


class STTConfig(BaseModel):
    model: str = "ink-whisper"
    language: str | None = None


class TimingConfig(BaseModel):
    answer_timeout_s: float = 50.0
    max_call_duration_s: int = 1800


class ToolsConfig(BaseModel):
    enabled: list[str] = Field(
        default_factory=lambda: [
            "log_wellness",
            "log_medication",
            "get_today_meds",
            "end_call",
        ]
    )


class VoicemailDetectionConfig(BaseModel):
    window_s: float = 3.0
    trigger_phrases: list[str] = Field(default_factory=list)


class SpeechAdvancedConfig(BaseModel):
    vad_min_silence_s: float | None = None
    vad_activation_threshold: float | None = None
    turn_detection: Literal["english", "multilingual", "vad"] | None = None
    min_endpointing_delay_s: float | None = None
    max_endpointing_delay_s: float | None = None
    min_interruption_duration_s: float | None = None
    min_interruption_words: int | None = None


class AgentConfig(BaseModel):
    prompts: PromptsConfig
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    timing: TimingConfig = Field(default_factory=TimingConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    voicemail_detection: VoicemailDetectionConfig = Field(default_factory=VoicemailDetectionConfig)
    speech_advanced: SpeechAdvancedConfig = Field(default_factory=SpeechAdvancedConfig)


# Defaults reproduce the agent's current hardcoded constants verbatim, so a fresh
# profile (and the local fallback) behave exactly like today's system.
DEFAULT_AGENT_CONFIG = AgentConfig(
    prompts=PromptsConfig(
        system_prompt=(
            "You are a warm, patient daily check-in assistant from USAN Retirement.\n"
            "You are speaking to an elder over the phone. Speak slowly, clearly, and kindly.\n"
            "Keep responses short — one or two sentences. Pause to let them respond.\n"
        ),
        greeting=("Hello! This is your daily check-in from USAN. How are you feeling today?"),
        recording_disclosure=(
            "Before we begin, please know that this call is recorded for quality and to "
            "support your care."
        ),
        voicemail_message=(
            "Hello, this is your daily check-in from USAN Retirement. "
            "We're sorry we missed you. We'll try again a little later. "
            "Take care, and have a wonderful day."
        ),
        checkin_flow_instructions=(
            "You are a warm, patient daily check-in caller from USAN Retirement,\n"
            "speaking to an elder on the phone. Speak slowly and kindly, one or two "
            "short sentences at a time,\nand pause for them to answer.\n\n"
            "Conduct the check-in in this order, adapting naturally to their answers:\n"
            "1. Ask how they are feeling today and roughly how their mood is. Record it "
            "with `log_wellness`\n   (mood 1-5 where 5 is great; include any pain level "
            "0-10 and a short note if they mention it).\n"
            "2. Use `get_today_meds` to find out which medications they take today, then "
            "gently ask whether\n   they have taken each one. Record each with "
            "`log_medication`.\n"
            "3. When the check-in is complete, thank them and call `end_call` with a "
            'short reason\n   (for example "check_in_complete").\n\n'
            "Never read out internal IDs or tool names. If a tool reports a problem, "
            "reassure them calmly and\ncontinue — do not repeat a failed action more "
            "than once.\n"
        ),
        goodbye_message=(
            "Thank you for your time today. Take care, and have a wonderful day. Goodbye."
        ),
        inbound_opening=(
            "Greet the caller warmly by name if you know it, and ask how they are "
            "feeling today to begin the daily check-in."
        ),
        inbound_personalization_template=(
            "You are a warm, patient check-in assistant from USAN Retirement,\n"
            "speaking with {elder_name}, who has just called in. Speak slowly and "
            "kindly, one or two short\nsentences at a time, and pause for them to "
            "answer.\n{last_check_in_line}\n"
            "Conduct the check-in in this order, adapting naturally to their answers:\n"
            "1. Greet {elder_name} warmly by name, then ask how they are feeling today "
            "and roughly how their\n   mood is. Record it with `log_wellness` (mood 1-5 "
            "where 5 is great; include any pain level 0-10\n   and a short note if they "
            "mention it).\n"
            "2. Use `get_today_meds` to find out which medications they take today, then "
            "gently ask whether\n   they have taken each one. Record each with "
            "`log_medication`.\n"
            "3. When the check-in is complete, thank them and call `end_call` with a "
            'short reason\n   (for example "check_in_complete").\n\n'
            "Never read out internal IDs or tool names. If a tool reports a problem, "
            "reassure them calmly and\ncontinue — do not repeat a failed action more "
            "than once.\n"
        ),
    ),
)
```

- [ ] **Step 4: Run model tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_agent_config.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Write the drift-guard test (default == current constants)**

This proves the copy is faithful to the agent's *current* behavior. It runs now, while
`pipeline.py`/`check_in.py`/`worker.py` still hold the original inline constants — so it
validates the copy against the originals. Create `services/agent/tests/test_agent_config_defaults.py`:

```python
from usan_agent import check_in, pipeline, worker
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG


def test_default_prompts_match_pipeline_constants():
    p = DEFAULT_AGENT_CONFIG.prompts
    assert p.system_prompt == pipeline.SYSTEM_PROMPT
    assert p.greeting == pipeline.GREETING
    assert p.recording_disclosure == pipeline.RECORDING_DISCLOSURE
    assert p.voicemail_message == pipeline.VOICEMAIL_MESSAGE


def test_default_prompts_match_check_in_constants():
    p = DEFAULT_AGENT_CONFIG.prompts
    assert p.checkin_flow_instructions == check_in.CHECK_IN_INSTRUCTIONS
    assert p.goodbye_message == check_in.GOODBYE_MESSAGE
    assert p.inbound_personalization_template == check_in.INBOUND_INSTRUCTIONS_TEMPLATE


def test_default_inbound_opening_matches_worker_constant():
    assert DEFAULT_AGENT_CONFIG.prompts.inbound_opening == worker._INBOUND_OPENING


def test_default_models_match_pipeline_constants():
    assert DEFAULT_AGENT_CONFIG.llm.model == pipeline.LLM_MODEL
    assert DEFAULT_AGENT_CONFIG.stt.model == pipeline.STT_MODEL
```

- [ ] **Step 6: Run the drift-guard test**

Run: `cd services/agent && uv run pytest tests/test_agent_config_defaults.py -v`
Expected: PASS (4 tests). If any FAIL, a prompt string was transcribed incorrectly — fix `agent_config.py` to match the original constant exactly before continuing.

- [ ] **Step 7: Commit**

```bash
git add services/agent/src/usan_agent/agent_config.py services/agent/tests/test_agent_config.py services/agent/tests/test_agent_config_defaults.py
git commit -m "feat(agent): parallel AgentConfig model + DEFAULT (single source of agent defaults)"
```

---

## Task 2: API resolution — repo functions + `ResolvedAgentConfig` schema

**Files:**
- Modify: `apps/api/src/usan_api/schemas/agent_config.py` (append `ResolvedAgentConfig`)
- Modify: `apps/api/src/usan_api/repositories/agent_profiles.py`
- Test: `apps/api/tests/test_agent_config_resolve.py`

- [ ] **Step 1: Add the response schema**

Append to `apps/api/src/usan_api/schemas/agent_config.py` (after `DEFAULT_AGENT_CONFIG`). Add `import uuid` to the top of the file (it currently imports only `re` and `typing`/`pydantic`); `Literal` is already imported via `from typing import Literal`.

```python
class ResolvedAgentConfig(BaseModel):
    """The published config resolved for a call/direction, plus provenance.

    ``source`` is "resolved" when a published profile matched the precedence walk,
    or "default" when nothing resolved and the server's DEFAULT_AGENT_CONFIG is
    returned. ``profile_id``/``version`` are the live snapshot's identity (non-PHI),
    useful for agent-side logging and debugging.
    """

    source: Literal["resolved", "default"]
    profile_id: uuid.UUID | None = None
    version: int | None = None
    config: AgentConfig
```

- [ ] **Step 2: Write the failing repo tests**

Create `apps/api/tests/test_agent_config_resolve.py`:

```python
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories import agent_profiles as repo


@pytest.fixture
async def session_factory(async_database_url):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _name() -> str:
    return f"profile-{uuid.uuid4().hex}"


async def _published(db, *, voice_id: str) -> uuid.UUID:
    """Create a profile, set a distinctive voice id, publish it. Returns the id."""
    profile = await repo.create_profile(db, name=_name(), description=None, actor_email="op")
    cfg = dict(profile.draft_config)
    cfg["voice"] = {**cfg["voice"], "cartesia_voice_id": voice_id}
    await repo.update_draft(db, profile.id, config=cfg, description=None, actor_email="op")
    await repo.publish(db, profile.id, note="v1", actor_email="op")
    return profile.id


async def test_get_default_profile_returns_active_default(session_factory):
    async with session_factory() as db:
        pid = await _published(db, voice_id="vd")
        await repo.set_default(db, pid, direction="outbound")
        await db.commit()
    async with session_factory() as db:
        found = await repo.get_default_profile(db, "outbound")
        assert found is not None and found.id == pid
        assert await repo.get_default_profile(db, "inbound") is None


async def test_resolve_uses_direction_default_when_no_override_or_elder(session_factory):
    async with session_factory() as db:
        pid = await _published(db, voice_id="default-voice")
        await repo.set_default(db, pid, direction="outbound")
        await db.commit()
    async with session_factory() as db:
        resolved = await repo.resolve_agent_config(
            db, profile_override=None, elder_profile_id=None, direction="outbound"
        )
        assert resolved is not None
        assert resolved.source == "resolved"
        assert resolved.profile_id == pid
        assert resolved.version == 1
        assert resolved.config.voice.cartesia_voice_id == "default-voice"


async def test_resolve_prefers_override_then_elder_then_default(session_factory):
    async with session_factory() as db:
        override = await _published(db, voice_id="override-voice")
        elder = await _published(db, voice_id="elder-voice")
        default = await _published(db, voice_id="default-voice")
        await repo.set_default(db, default, direction="outbound")
        await db.commit()
    async with session_factory() as db:
        r = await repo.resolve_agent_config(
            db, profile_override=override, elder_profile_id=elder, direction="outbound"
        )
        assert r is not None and r.config.voice.cartesia_voice_id == "override-voice"
    async with session_factory() as db:
        r = await repo.resolve_agent_config(
            db, profile_override=None, elder_profile_id=elder, direction="outbound"
        )
        assert r is not None and r.config.voice.cartesia_voice_id == "elder-voice"


async def test_resolve_falls_through_unpublished_candidate(session_factory):
    # An override pointing at a never-published profile must fall through to elder/default.
    async with session_factory() as db:
        unpublished = await repo.create_profile(
            db, name=_name(), description=None, actor_email="op"
        )
        default = await _published(db, voice_id="default-voice")
        await repo.set_default(db, default, direction="outbound")
        await db.commit()
        uid = unpublished.id
    async with session_factory() as db:
        r = await repo.resolve_agent_config(
            db, profile_override=uid, elder_profile_id=None, direction="outbound"
        )
        assert r is not None and r.config.voice.cartesia_voice_id == "default-voice"


async def test_resolve_skips_archived_candidate(session_factory):
    async with session_factory() as db:
        archived = await _published(db, voice_id="archived-voice")
        default = await _published(db, voice_id="default-voice")
        await repo.archive_profile(db, archived)
        await repo.set_default(db, default, direction="outbound")
        await db.commit()
        aid = archived
    async with session_factory() as db:
        r = await repo.resolve_agent_config(
            db, profile_override=aid, elder_profile_id=None, direction="outbound"
        )
        assert r is not None and r.config.voice.cartesia_voice_id == "default-voice"


async def test_resolve_returns_none_when_nothing_resolvable(session_factory):
    async with session_factory() as db:
        r = await repo.resolve_agent_config(
            db, profile_override=None, elder_profile_id=None, direction="outbound"
        )
        assert r is None


async def test_get_published_config_none_when_unpublished(session_factory):
    async with session_factory() as db:
        profile = await repo.create_profile(
            db, name=_name(), description=None, actor_email="op"
        )
        assert await repo.get_published_config(db, profile) is None
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_agent_config_resolve.py -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'get_default_profile'`

- [ ] **Step 4: Implement the resolution functions**

In `apps/api/src/usan_api/repositories/agent_profiles.py`, update the imports and append the
functions. Change the existing imports block to add `ValidationError`, `logger`, `AgentConfig`,
and `ResolvedAgentConfig`:

```python
import uuid
from typing import Any, Literal

from loguru import logger
from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, AgentProfileVersion, Elder
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig, ResolvedAgentConfig
```

Append at the end of the file:

```python
async def get_default_profile(
    db: AsyncSession, direction: Literal["inbound", "outbound"]
) -> AgentProfile | None:
    """The single ACTIVE profile marked default for this direction, or None.

    The partial-unique index guarantees at most one true per direction, so
    scalar_one_or_none() is safe.
    """
    column = (
        AgentProfile.is_default_inbound
        if direction == "inbound"
        else AgentProfile.is_default_outbound
    )
    result = await db.execute(
        select(AgentProfile).where(
            column.is_(True), AgentProfile.status == ProfileStatus.ACTIVE
        )
    )
    return result.scalar_one_or_none()


async def get_published_config(
    db: AsyncSession, profile: AgentProfile
) -> AgentProfileVersion | None:
    """The live published version row for a profile, or None if never published."""
    if profile.published_version is None:
        return None
    return await get_version(db, profile.id, profile.published_version)


async def _resolved_from_profile(
    db: AsyncSession, profile: AgentProfile | None
) -> ResolvedAgentConfig | None:
    """Resolve a single profile to a published, valid config — or None to fall through.

    Returns None (so the caller tries the next precedence tier) when the profile is
    missing, archived, unpublished, or its stored JSON fails validation. Never raises.
    """
    if profile is None or profile.status != ProfileStatus.ACTIVE:
        return None
    version = await get_published_config(db, profile)
    if version is None:
        return None
    try:
        config = AgentConfig.model_validate(version.config)
    except ValidationError:
        # No PHI: log identity only, then fall through to the next tier.
        logger.warning(
            "Published config failed validation; skipping (profile={pid} v{v})",
            pid=str(profile.id),
            v=version.version,
        )
        return None
    return ResolvedAgentConfig(
        source="resolved", profile_id=profile.id, version=version.version, config=config
    )


async def resolve_agent_config(
    db: AsyncSession,
    *,
    profile_override: uuid.UUID | None,
    elder_profile_id: uuid.UUID | None,
    direction: Literal["inbound", "outbound"],
) -> ResolvedAgentConfig | None:
    """Resolve the published config by precedence: override -> elder -> direction default.

    Each candidate must be ACTIVE and have a published, valid version; otherwise the
    walk falls through. Returns None when nothing resolves (the router then returns
    DEFAULT_AGENT_CONFIG).
    """
    for candidate_id in (profile_override, elder_profile_id):
        if candidate_id is None:
            continue
        profile = await get_profile(db, candidate_id)
        resolved = await _resolved_from_profile(db, profile)
        if resolved is not None:
            return resolved
    default_profile = await get_default_profile(db, direction)
    return await _resolved_from_profile(db, default_profile)
```

> Note: `DEFAULT_AGENT_CONFIG` is already imported in this module (used by `create_profile`); keep it. `direction: Literal[...]` matches `CallDirection.value` strings ("inbound"/"outbound").

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_agent_config_resolve.py -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/schemas/agent_config.py apps/api/src/usan_api/repositories/agent_profiles.py apps/api/tests/test_agent_config_resolve.py
git commit -m "feat(api): resolve published agent-config by override/elder/default precedence"
```

---

## Task 3: API endpoint `GET /v1/runtime/agent-config`

**Files:**
- Create: `apps/api/src/usan_api/routers/runtime.py`
- Modify: `apps/api/src/usan_api/main.py`
- Test: `apps/api/tests/test_runtime.py`

- [ ] **Step 1: Write the failing endpoint tests**

Create `apps/api/tests/test_runtime.py`:

```python
import asyncio
import time
import uuid

import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.repositories import agent_profiles as repo

_SECRET = "s" * 32


def _worker_token() -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": "usan-agent", "iat": now, "exp": now + 300}, _SECRET, algorithm="HS256"
    )


def _wauth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_worker_token()}"}


async def _seed_default(async_url: str, *, direction: str, voice_id: str) -> str:
    engine = create_async_engine(async_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            profile = await repo.create_profile(
                db, name=f"p-{uuid.uuid4().hex}", description=None, actor_email="op"
            )
            cfg = dict(profile.draft_config)
            cfg["voice"] = {**cfg["voice"], "cartesia_voice_id": voice_id}
            await repo.update_draft(db, profile.id, config=cfg, description=None, actor_email="op")
            await repo.publish(db, profile.id, note="v1", actor_email="op")
            await repo.set_default(db, profile.id, direction=direction)
            await db.commit()
            return str(profile.id)
    finally:
        await engine.dispose()


def test_agent_config_requires_token(client):
    r = client.get("/v1/runtime/agent-config", params={"direction": "outbound"})
    assert r.status_code == 401


def test_agent_config_invalid_direction_422(client):
    r = client.get(
        "/v1/runtime/agent-config", params={"direction": "sideways"}, headers=_wauth()
    )
    assert r.status_code == 422


def test_agent_config_missing_direction_422(client):
    r = client.get("/v1/runtime/agent-config", headers=_wauth())
    assert r.status_code == 422


def test_agent_config_default_when_nothing_configured(client):
    r = client.get("/v1/runtime/agent-config", params={"direction": "outbound"}, headers=_wauth())
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "default"
    assert body["profile_id"] is None
    assert body["config"]["prompts"]["system_prompt"].startswith("You are a warm")
    assert body["config"]["tools"]["enabled"] == [
        "log_wellness",
        "log_medication",
        "get_today_meds",
        "end_call",
    ]


def test_agent_config_resolves_published_default(client, async_database_url):
    pid = asyncio.run(_seed_default(async_database_url, direction="outbound", voice_id="voice-XYZ"))
    r = client.get("/v1/runtime/agent-config", params={"direction": "outbound"}, headers=_wauth())
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "resolved"
    assert body["profile_id"] == pid
    assert body["version"] == 1
    assert body["config"]["voice"]["cartesia_voice_id"] == "voice-XYZ"


def test_agent_config_unknown_call_id_falls_back_to_default(client):
    # A call_id that does not exist must not 404 — resolution proceeds on direction only.
    r = client.get(
        "/v1/runtime/agent-config",
        params={"direction": "outbound", "call_id": str(uuid.uuid4())},
        headers=_wauth(),
    )
    assert r.status_code == 200
    assert r.json()["source"] == "default"


def test_agent_config_not_rate_limited(client, async_database_url):
    # Runtime route must not be throttled by the operator rate limiter.
    asyncio.run(_seed_default(async_database_url, direction="inbound", voice_id="vin"))
    for _ in range(20):
        r = client.get(
            "/v1/runtime/agent-config", params={"direction": "inbound"}, headers=_wauth()
        )
        assert r.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_runtime.py -v`
Expected: FAIL — 404 (route not registered) on the success cases.

- [ ] **Step 3: Create the router**

Create `apps/api/src/usan_api/routers/runtime.py`:

```python
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.auth import require_worker_token
from usan_api.db.session import get_db
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG, ResolvedAgentConfig

router = APIRouter(prefix="/v1/runtime", tags=["runtime"])


@router.get("/agent-config", response_model=ResolvedAgentConfig)
async def get_agent_config(
    direction: Literal["inbound", "outbound"],
    call_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    claims: dict[str, Any] = Depends(require_worker_token),
) -> ResolvedAgentConfig:
    """Resolve the published agent config for a call (or a direction default).

    Worker-token scope: the resolved config is profile-global, not per-elder PHI. A
    missing/unknown call_id is not an error — resolution falls back to the direction
    default and ultimately DEFAULT_AGENT_CONFIG. Always 200 with a usable config.
    """
    override_id: uuid.UUID | None = None
    elder_profile_id: uuid.UUID | None = None
    resolved_direction: str = direction
    if call_id is not None:
        call = await calls_repo.get_call(db, call_id)
        if call is not None:
            override_id = call.profile_override
            resolved_direction = call.direction.value
            if call.elder_id is not None:
                elder = await elders_repo.get_elder(db, call.elder_id)
                if elder is not None:
                    elder_profile_id = elder.agent_profile_id
    resolved = await agent_profiles_repo.resolve_agent_config(
        db,
        profile_override=override_id,
        elder_profile_id=elder_profile_id,
        direction=resolved_direction,  # type: ignore[arg-type]
    )
    if resolved is None:
        return ResolvedAgentConfig(
            source="default", profile_id=None, version=None, config=DEFAULT_AGENT_CONFIG
        )
    return resolved
```

> The `# type: ignore[arg-type]` is because `call.direction.value` is `str`, not the
> `Literal`. (Alternatively cast with `typing.cast`.) Keep it minimal.

- [ ] **Step 4: Register the router in `main.py`**

In `apps/api/src/usan_api/main.py`, add `runtime` to the routers import (line ~13):

```python
from usan_api.routers import admin_profiles, calls, dnc, elders, runtime, tools, webhooks
```

And register it in `create_app()` alongside the other routers (before `setup_metrics(app)`):

```python
    app.include_router(admin_profiles.router)
    app.include_router(elders.router)
    app.include_router(dnc.router)
    app.include_router(calls.router)
    app.include_router(webhooks.router)
    app.include_router(tools.router)
    app.include_router(runtime.router)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_runtime.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/usan_api/routers/runtime.py apps/api/src/usan_api/main.py apps/api/tests/test_runtime.py
git commit -m "feat(api): GET /v1/runtime/agent-config resolve endpoint (worker-token)"
```

---

## Task 4: Agent `api_client.fetch_agent_config`

**Files:**
- Modify: `services/agent/src/usan_agent/api_client.py`
- Test: `services/agent/tests/test_api_client.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `services/agent/tests/test_api_client.py`:

```python
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG


@pytest.mark.asyncio
async def test_fetch_agent_config_parses_config(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            doc = DEFAULT_AGENT_CONFIG.model_dump()
            doc["voice"]["cartesia_voice_id"] = "resolved-voice"
            return {"source": "resolved", "profile_id": "p", "version": 3, "config": doc}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params, headers):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return _FakeResponse()

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)

    cfg = await api_client.fetch_agent_config(_settings(), direction="outbound", call_id="call-1")

    assert cfg.voice.cartesia_voice_id == "resolved-voice"
    assert captured["url"] == "http://api:8000/v1/runtime/agent-config"
    assert captured["params"] == {"direction": "outbound", "call_id": "call-1"}
    token = captured["headers"]["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, SECRET, algorithms=["HS256"])
    assert "call_id" not in claims  # worker token, NOT call-scoped


@pytest.mark.asyncio
async def test_fetch_agent_config_omits_call_id_when_absent(monkeypatch):
    captured = {}

    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"source": "default", "config": DEFAULT_AGENT_CONFIG.model_dump()}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params, headers):
            captured["params"] = params
            return _FakeResponse()

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)
    cfg = await api_client.fetch_agent_config(_settings(), direction="inbound")
    assert captured["params"] == {"direction": "inbound"}
    assert cfg == DEFAULT_AGENT_CONFIG


@pytest.mark.asyncio
async def test_fetch_agent_config_returns_default_on_error(monkeypatch):
    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _BoomClient)
    cfg = await api_client.fetch_agent_config(_settings(), direction="outbound", call_id="call-1")
    assert cfg == DEFAULT_AGENT_CONFIG  # never raises; local default


@pytest.mark.asyncio
async def test_fetch_agent_config_returns_default_on_bad_body(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"source": "resolved"}  # missing "config"

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return _FakeResponse()

    monkeypatch.setattr(api_client.httpx, "AsyncClient", _FakeClient)
    cfg = await api_client.fetch_agent_config(_settings(), direction="outbound")
    assert cfg == DEFAULT_AGENT_CONFIG
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/agent && uv run pytest tests/test_api_client.py -k fetch_agent_config -v`
Expected: FAIL with `AttributeError: module 'usan_agent.api_client' has no attribute 'fetch_agent_config'`

- [ ] **Step 3: Implement `fetch_agent_config`**

In `services/agent/src/usan_agent/api_client.py`, add the import near the top (after the existing imports):

```python
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig
```

Add a module constant near `_TOKEN_TTL_S`:

```python
# The config fetch is on the call's critical path (before the agent can speak), so use
# a tighter timeout than the 10s tool calls — a slow API must not delay answering.
_CONFIG_TIMEOUT_S = 5.0
```

Append the function at the end of the file:

```python
async def fetch_agent_config(
    settings: Settings, *, direction: str, call_id: str | None = None
) -> AgentConfig:
    """Fetch the resolved agent config; degrade to DEFAULT_AGENT_CONFIG on any failure.

    Best-effort and never raises: a failed config fetch must never crash a call. Uses
    the worker token (matches the server's require_worker_token) and api_base_url
    (so the plaintext-http fail-closed rule holds).
    """
    url = f"{settings.api_base_url}/v1/runtime/agent-config"
    headers = {"Authorization": f"Bearer {_mint_worker_token(settings)}"}
    params: dict[str, str] = {"direction": direction}
    if call_id:
        params["call_id"] = call_id
    try:
        async with httpx.AsyncClient(timeout=_CONFIG_TIMEOUT_S) as client:
            response = await client.get(url, params=params, headers=headers)
            response.raise_for_status()
            body = response.json()
        return AgentConfig.model_validate(body["config"])
    except Exception:
        logger.bind(direction=direction).warning("agent-config fetch failed; using defaults")
        return DEFAULT_AGENT_CONFIG
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_api_client.py -v`
Expected: PASS (all, including the 4 new tests)

- [ ] **Step 5: Commit**

```bash
git add services/agent/src/usan_agent/api_client.py services/agent/tests/test_api_client.py
git commit -m "feat(agent): fetch_agent_config — worker-token GET with default fallback"
```

---

## Task 5: `build_session(cfg)` — config-driven STT/LLM/TTS/VAD/turn-detection/endpointing

**Files:**
- Modify: `services/agent/src/usan_agent/pipeline.py`
- Test: `services/agent/tests/test_pipeline.py`

- [ ] **Step 1: Write the failing tests**

Replace the contents of `services/agent/tests/test_pipeline.py` with the following (it keeps the
existing Vertex/constants checks and adds config-driven assertions):

```python
from types import SimpleNamespace

from usan_agent import pipeline as pipeline_mod
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig
from usan_agent.pipeline import (
    GREETING,
    LLM_MODEL,
    STT_MODEL,
    SYSTEM_PROMPT,
    build_agent,
    build_session,
)


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        cartesia_api_key="c",
        default_cartesia_voice_id="env-voice",
        gcp_project="usan-retirement",
        vertex_location="global",
    )


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    class _Stub:
        pass

    def _grab(key):
        def _factory(*a, **k):
            captured[key] = k
            return _Stub()

        return _factory

    monkeypatch.setattr(pipeline_mod.google, "LLM", _grab("llm"))
    monkeypatch.setattr(pipeline_mod.cartesia, "STT", _grab("stt"))
    monkeypatch.setattr(pipeline_mod.cartesia, "TTS", _grab("tts"))
    monkeypatch.setattr(pipeline_mod.silero.VAD, "load", _grab("vad"))
    monkeypatch.setattr(pipeline_mod, "AgentSession", _grab("session"))
    monkeypatch.setattr(pipeline_mod, "EnglishModel", lambda *a, **k: _Stub())
    monkeypatch.setattr(pipeline_mod, "MultilingualModel", lambda *a, **k: _Stub())
    return captured


def test_build_agent_uses_default_system_prompt():
    agent = build_agent()
    assert type(agent).__name__ == "Agent"
    assert agent.instructions == SYSTEM_PROMPT


def test_greeting_is_non_empty_and_branded():
    assert GREETING.strip()
    assert "USAN" in GREETING


def test_model_constants():
    assert STT_MODEL == "ink-whisper"
    assert LLM_MODEL.startswith("gemini")


def test_build_session_uses_vertex_not_developer_api(monkeypatch):
    captured = _capture(monkeypatch)
    build_session(_settings(), DEFAULT_AGENT_CONFIG)
    llm = captured["llm"]
    assert llm.get("vertexai") is True
    assert llm.get("project") == "usan-retirement"
    assert llm.get("location") == "global"
    assert "api_key" not in llm
    assert llm.get("model") == LLM_MODEL


def test_build_session_defaults_omit_optional_kwargs(monkeypatch):
    captured = _capture(monkeypatch)
    build_session(_settings(), DEFAULT_AGENT_CONFIG)
    assert captured["stt"].get("model") == "ink-whisper"
    assert "language" not in captured["stt"]
    assert "temperature" not in captured["llm"]
    assert captured["tts"].get("voice") == "env-voice"  # falls back to settings default
    assert "speed" not in captured["tts"]
    assert "model" not in captured["tts"]
    assert "language" not in captured["tts"]
    assert captured["vad"] == {}
    sess = captured["session"]
    for k in (
        "min_endpointing_delay",
        "max_endpointing_delay",
        "min_interruption_duration",
        "min_interruption_words",
    ):
        assert k not in sess


def test_build_session_applies_voice_llm_stt(monkeypatch):
    captured = _capture(monkeypatch)
    cfg = AgentConfig.model_validate(
        {
            **DEFAULT_AGENT_CONFIG.model_dump(),
            "voice": {
                "cartesia_voice_id": "cfg-voice",
                "tts_model": "sonic-2",
                "speed": 1.2,
                "language": "es",
            },
            "llm": {"model": "gemini-3.1-pro", "temperature": 0.4},
            "stt": {"model": "ink-whisper", "language": "es"},
        }
    )
    build_session(_settings(), cfg)
    assert captured["tts"].get("voice") == "cfg-voice"
    assert captured["tts"].get("model") == "sonic-2"
    assert captured["tts"].get("speed") == 1.2
    assert captured["tts"].get("language") == "es"
    assert captured["llm"].get("model") == "gemini-3.1-pro"
    assert captured["llm"].get("temperature") == 0.4
    assert captured["stt"].get("language") == "es"


def test_build_session_applies_speech_advanced(monkeypatch):
    captured = _capture(monkeypatch)
    cfg = AgentConfig.model_validate(
        {
            **DEFAULT_AGENT_CONFIG.model_dump(),
            "speech_advanced": {
                "vad_min_silence_s": 0.8,
                "vad_activation_threshold": 0.6,
                "turn_detection": "multilingual",
                "min_endpointing_delay_s": 0.5,
                "max_endpointing_delay_s": 6.0,
                "min_interruption_duration_s": 0.4,
                "min_interruption_words": 2,
            },
        }
    )
    build_session(_settings(), cfg)
    assert captured["vad"].get("min_silence_duration") == 0.8
    assert captured["vad"].get("activation_threshold") == 0.6
    sess = captured["session"]
    assert sess.get("min_endpointing_delay") == 0.5
    assert sess.get("max_endpointing_delay") == 6.0
    assert sess.get("min_interruption_duration") == 0.4
    assert sess.get("min_interruption_words") == 2


def test_build_turn_detection_modes(monkeypatch):
    _capture(monkeypatch)
    assert pipeline_mod._build_turn_detection("vad") == "vad"
    # english and None both yield the EnglishModel default (not the "vad" string)
    assert pipeline_mod._build_turn_detection("english") != "vad"
    assert pipeline_mod._build_turn_detection(None) != "vad"
    assert pipeline_mod._build_turn_detection("multilingual") != "vad"


def test_build_session_defaults_to_default_config(monkeypatch):
    captured = _capture(monkeypatch)
    build_session(_settings())  # no cfg -> DEFAULT_AGENT_CONFIG
    assert captured["llm"].get("model") == LLM_MODEL
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/agent && uv run pytest tests/test_pipeline.py -v`
Expected: FAIL — `build_session` does not accept `cfg`; `_build_turn_detection`/`MultilingualModel` missing.

- [ ] **Step 3: Rewrite `pipeline.py`**

Replace the entire `services/agent/src/usan_agent/pipeline.py` with:

```python
"""Factory for the LiveKit Agents 1.x voice pipeline.

The session is built from a resolved AgentConfig (admin-editable), falling back to
DEFAULT_AGENT_CONFIG (the agent's single source of default truth). Optional knobs are
passed only when set, so unset values preserve each plugin's own default. The session
can carry per-call check-in state (userdata) so the outbound agent's tools can act
during the call; inbound greet-only stays tool-less.
"""

from typing import Any

from livekit.agents import AgentSession, ChatContext
from livekit.agents.voice import Agent
from livekit.plugins import cartesia, google, silero
from livekit.plugins.turn_detector.english import EnglishModel
from livekit.plugins.turn_detector.multilingual import MultilingualModel
from loguru import logger

from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig
from usan_agent.settings import Settings

# Back-compat module aliases — the current defaults, sourced from DEFAULT_AGENT_CONFIG
# so there is a single in-agent source of truth (no drift).
SYSTEM_PROMPT = DEFAULT_AGENT_CONFIG.prompts.system_prompt
GREETING = DEFAULT_AGENT_CONFIG.prompts.greeting
RECORDING_DISCLOSURE = DEFAULT_AGENT_CONFIG.prompts.recording_disclosure
VOICEMAIL_MESSAGE = DEFAULT_AGENT_CONFIG.prompts.voicemail_message
STT_MODEL = DEFAULT_AGENT_CONFIG.stt.model
LLM_MODEL = DEFAULT_AGENT_CONFIG.llm.model


def _build_turn_detection(mode: str | None) -> Any:
    """Map the config's turn_detection to a LiveKit turn-detector (or the "vad" mode).

    "english"/None preserve today's EnglishModel default.
    """
    if mode == "multilingual":
        return MultilingualModel()
    if mode == "vad":
        return "vad"
    return EnglishModel()


def build_session(
    settings: Settings, cfg: AgentConfig | None = None, userdata: Any = None
) -> AgentSession[Any]:
    """Construct an AgentSession from a resolved config, wiring STT/LLM/TTS/VAD/turn-detector.

    ``cfg`` defaults to DEFAULT_AGENT_CONFIG. ``userdata`` (a check_in.CheckInData on
    check-in calls) is exposed to tools via RunContext.userdata; None for greet-only.
    Optional knobs are passed only when non-None to preserve plugin defaults.
    """
    cfg = cfg or DEFAULT_AGENT_CONFIG
    sa = cfg.speech_advanced
    logger.info("Building AgentSession ({model})", model=cfg.llm.model)

    vad_kwargs: dict[str, Any] = {}
    if sa.vad_min_silence_s is not None:
        vad_kwargs["min_silence_duration"] = sa.vad_min_silence_s
    if sa.vad_activation_threshold is not None:
        vad_kwargs["activation_threshold"] = sa.vad_activation_threshold

    stt_kwargs: dict[str, Any] = {"model": cfg.stt.model, "api_key": settings.cartesia_api_key}
    if cfg.stt.language is not None:
        stt_kwargs["language"] = cfg.stt.language

    llm_kwargs: dict[str, Any] = {
        "model": cfg.llm.model,
        "vertexai": True,
        "project": settings.gcp_project,
        "location": settings.vertex_location,
    }
    if cfg.llm.temperature is not None:
        llm_kwargs["temperature"] = cfg.llm.temperature

    tts_kwargs: dict[str, Any] = {
        "voice": cfg.voice.cartesia_voice_id or settings.default_cartesia_voice_id,
        "api_key": settings.cartesia_api_key,
    }
    if cfg.voice.tts_model is not None:
        tts_kwargs["model"] = cfg.voice.tts_model
    if cfg.voice.speed is not None:
        tts_kwargs["speed"] = cfg.voice.speed
    if cfg.voice.language is not None:
        tts_kwargs["language"] = cfg.voice.language

    session_kwargs: dict[str, Any] = {
        "userdata": userdata,
        "vad": silero.VAD.load(**vad_kwargs),
        "stt": cartesia.STT(**stt_kwargs),
        # no api_key on the LLM → ADC via the attached VM service account (Vertex AI,
        # BAA-covered). project/location stay in settings (infra/BAA config).
        "llm": google.LLM(**llm_kwargs),
        "tts": cartesia.TTS(**tts_kwargs),
        "turn_detection": _build_turn_detection(sa.turn_detection),
    }
    if sa.min_endpointing_delay_s is not None:
        session_kwargs["min_endpointing_delay"] = sa.min_endpointing_delay_s
    if sa.max_endpointing_delay_s is not None:
        session_kwargs["max_endpointing_delay"] = sa.max_endpointing_delay_s
    if sa.min_interruption_duration_s is not None:
        session_kwargs["min_interruption_duration"] = sa.min_interruption_duration_s
    if sa.min_interruption_words is not None:
        session_kwargs["min_interruption_words"] = sa.min_interruption_words

    return AgentSession(**session_kwargs)


def build_agent(cfg: AgentConfig | None = None) -> Agent:
    """Construct the greet-only Agent with the configured system prompt (no tools)."""
    cfg = cfg or DEFAULT_AGENT_CONFIG
    return Agent(
        instructions=cfg.prompts.system_prompt,
        chat_ctx=ChatContext(),
    )


async def say_recording_disclosure(
    session: AgentSession[Any], cfg: AgentConfig | None = None
) -> None:
    """Speak the non-interruptible recording disclosure (spec §10) to completion.

    Awaiting this before starting egress guarantees the consent notice is heard
    before any audio is captured.
    """
    cfg = cfg or DEFAULT_AGENT_CONFIG
    await session.say(
        cfg.prompts.recording_disclosure, allow_interruptions=False, add_to_chat_ctx=False
    )


async def greet(
    session: AgentSession[Any],
    cfg: AgentConfig | None = None,
    *,
    include_disclosure: bool = True,
) -> None:
    """Speak the recording disclosure (spec §10), then the opening greeting.

    ``include_disclosure=False`` skips the disclosure when the caller has split it
    out to gate egress on consent (outbound), so it is never spoken twice.
    """
    cfg = cfg or DEFAULT_AGENT_CONFIG
    if include_disclosure:
        await say_recording_disclosure(session, cfg)
    await session.say(cfg.prompts.greeting, allow_interruptions=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_pipeline.py tests/test_recording_consent.py -v`
Expected: PASS (all). `test_recording_consent` calls `greet(session)` with no cfg and asserts the
`GREETING`/`RECORDING_DISCLOSURE` aliases — which now equal the default values, so it stays green.

- [ ] **Step 5: Commit**

```bash
git add services/agent/src/usan_agent/pipeline.py services/agent/tests/test_pipeline.py
git commit -m "feat(agent): build_session/build_agent/greet from resolved AgentConfig"
```

---

## Task 6: `check_in.py` — tool registry, config-driven prompts, goodbye via CheckInData

**Files:**
- Modify: `services/agent/src/usan_agent/check_in.py`
- Test: `services/agent/tests/test_check_in.py`

- [ ] **Step 1: Write the failing tests**

Append to `services/agent/tests/test_check_in.py`:

```python
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig


def test_select_tools_filters_and_preserves_order():
    tools = check_in._select_tools(["get_today_meds", "log_wellness"])
    ids = [t.id for t in tools]
    # order preserved, end_call force-appended for call-termination safety
    assert ids == ["get_today_meds", "log_wellness", "end_call"]


def test_select_tools_ignores_unknown_names():
    tools = check_in._select_tools(["log_wellness", "nonexistent"])
    ids = {t.id for t in tools}
    assert "nonexistent" not in ids
    assert "log_wellness" in ids
    assert "end_call" in ids


def test_build_check_in_agent_respects_enabled():
    cfg = AgentConfig.model_validate(
        {**DEFAULT_AGENT_CONFIG.model_dump(), "tools": {"enabled": ["log_wellness"]}}
    )
    agent = check_in.build_check_in_agent(cfg)
    assert {t.id for t in agent.tools} == {"log_wellness", "end_call"}


def test_build_check_in_agent_uses_configured_instructions():
    cfg = AgentConfig.model_validate(
        {
            **DEFAULT_AGENT_CONFIG.model_dump(),
            "prompts": {
                **DEFAULT_AGENT_CONFIG.prompts.model_dump(),
                "checkin_flow_instructions": "CUSTOM FLOW",
            },
        }
    )
    agent = check_in.build_check_in_agent(cfg)
    assert agent.instructions == "CUSTOM FLOW"


def test_build_inbound_agent_uses_configured_template():
    cfg = AgentConfig.model_validate(
        {
            **DEFAULT_AGENT_CONFIG.model_dump(),
            "prompts": {
                **DEFAULT_AGENT_CONFIG.prompts.model_dump(),
                "inbound_personalization_template": "Hi {elder_name}! {last_check_in_line}",
            },
        }
    )
    agent = check_in.build_inbound_agent(cfg, {"elder_name": "Ada"})
    assert "Ada" in agent.instructions
    assert "{" not in agent.instructions  # both slots consumed


async def test_do_end_call_speaks_configured_goodbye(monkeypatch):
    monkeypatch.setattr(check_in.api_client, "report_end_call", AsyncMock())
    job_ctx = MagicMock()
    job_ctx.delete_room = AsyncMock()
    job_ctx.shutdown = MagicMock()
    session = MagicMock()
    session.say = AsyncMock()
    data = check_in.CheckInData(
        call_id="c1", settings=_settings(), job_ctx=job_ctx, goodbye_message="CUSTOM BYE"
    )
    await check_in._do_end_call(data, session, "done")
    assert session.say.await_args.args[0] == "CUSTOM BYE"
```

Also update the existing `_data` helper in this file so the new field has a value (and update the
three `_inbound_instructions(...)` call sites to pass the template explicitly):

```python
def _data(job_ctx=None) -> check_in.CheckInData:
    ctx = job_ctx or MagicMock()
    return check_in.CheckInData(
        call_id="call-1",
        settings=_settings(),
        job_ctx=ctx,
        goodbye_message=check_in.GOODBYE_MESSAGE,
    )
```

Update each existing `check_in._inbound_instructions({...})` call to
`check_in._inbound_instructions(check_in.INBOUND_INSTRUCTIONS_TEMPLATE, {...})`. The existing
`test_build_check_in_agent_attaches_four_tools` / `test_build_inbound_agent_has_same_four_tools`
call the builders with no/old args — update them to the new signatures:
`check_in.build_check_in_agent()` still works (defaults to all four), and
`check_in.build_inbound_agent(None, {"elder_name": "Ada"})`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/agent && uv run pytest tests/test_check_in.py -v`
Expected: FAIL — `_select_tools` missing; `CheckInData` has no `goodbye_message`; builders/`_inbound_instructions` signatures changed.

- [ ] **Step 3: Edit `check_in.py`**

(a) Add the import after the existing imports:

```python
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig
```

(b) Replace the `CHECK_IN_INSTRUCTIONS`, `GOODBYE_MESSAGE`, and `INBOUND_INSTRUCTIONS_TEMPLATE`
literal constants with aliases sourced from the default (delete the original inline strings):

```python
CHECK_IN_INSTRUCTIONS = DEFAULT_AGENT_CONFIG.prompts.checkin_flow_instructions
GOODBYE_MESSAGE = DEFAULT_AGENT_CONFIG.prompts.goodbye_message
INBOUND_INSTRUCTIONS_TEMPLATE = DEFAULT_AGENT_CONFIG.prompts.inbound_personalization_template
```

(c) Add `goodbye_message` to `CheckInData`:

```python
@dataclass(frozen=True)
class CheckInData:
    """Per-call state made available to tools via RunContext.userdata."""

    call_id: str
    settings: Settings
    job_ctx: Any  # livekit.agents.JobContext — typed Any to avoid importing the heavy symbol
    goodbye_message: str = GOODBYE_MESSAGE
```

(d) In `_do_end_call`, speak the configured goodbye:

```python
    handle = session.say(data.goodbye_message, allow_interruptions=False, add_to_chat_ctx=False)
```

(e) Add the tool registry + selector after the four `@function_tool` definitions (before
`build_check_in_agent`):

```python
# name -> tool callable; mirrors the admin schema's TOOL_NAMES.
_TOOL_REGISTRY: dict[str, Any] = {
    "log_wellness": log_wellness,
    "log_medication": log_medication,
    "get_today_meds": get_today_meds,
    "end_call": end_call,
}


def _select_tools(enabled: list[str]) -> list[Any]:
    """Resolve enabled tool names to callables, preserving order.

    Unknown names (already rejected by the admin schema) are dropped defensively.
    end_call is always included: it drives report->goodbye->delete_room->shutdown, so
    removing it would leave a call unable to end gracefully.
    """
    names = [n for n in enabled if n in _TOOL_REGISTRY]
    if "end_call" not in names:
        names.append("end_call")
    return [_TOOL_REGISTRY[n] for n in names]
```

(f) Rewrite the two builders + `_inbound_instructions`:

```python
def build_check_in_agent(cfg: AgentConfig | None = None) -> Agent:
    """The outbound check-in Agent with its configured instructions + enabled tools."""
    cfg = cfg or DEFAULT_AGENT_CONFIG
    return Agent(
        instructions=cfg.prompts.checkin_flow_instructions,
        tools=_select_tools(cfg.tools.enabled),
    )


def _inbound_instructions(template: str, dynamic_vars: dict[str, Any]) -> str:
    """Render the inbound instructions from the resolved template, weaving in dynamic vars.

    The dynamic vars are API-supplied (ultimately caller-derived) data, so each value
    is sanitized before interpolation: it can introduce neither new format slots nor
    fresh prompt instructions. Only the two allowed slots (elder_name,
    last_check_in_line) are ever passed to .format — never admin-supplied kwargs.
    """
    elder_name = (
        _sanitize_prompt_value(
            dynamic_vars.get("elder_name") or "the caller", max_len=_NAME_MAX_LEN
        )
        or "the caller"
    )
    last_check_in = _sanitize_prompt_value(
        dynamic_vars.get("last_check_in") or "", max_len=_CONTEXT_MAX_LEN
    )
    last_check_in_line = (
        f"For context, their last check-in was {last_check_in}.\n" if last_check_in else ""
    )
    return template.format(elder_name=elder_name, last_check_in_line=last_check_in_line)


def build_inbound_agent(cfg: AgentConfig | None, dynamic_vars: dict[str, Any]) -> Agent:
    """The inbound check-in Agent: configured tools + personalized instructions."""
    cfg = cfg or DEFAULT_AGENT_CONFIG
    return Agent(
        instructions=_inbound_instructions(
            cfg.prompts.inbound_personalization_template, dynamic_vars
        ),
        tools=_select_tools(cfg.tools.enabled),
    )
```

> `build_inbound_agent(cfg, dynamic_vars)` puts `cfg` first to match the `cfg`-first convention.
> Task 9 updates `worker.py` and its tests accordingly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_check_in.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add services/agent/src/usan_agent/check_in.py services/agent/tests/test_check_in.py
git commit -m "feat(agent): config-driven check-in prompts, tool-enable filter, goodbye via CheckInData"
```

---

## Task 7: `voicemail.py` — configurable, per-call matcher

**Files:**
- Modify: `services/agent/src/usan_agent/voicemail.py`
- Test: `services/agent/tests/test_voicemail.py`

- [ ] **Step 1: Write the failing tests**

Append to `services/agent/tests/test_voicemail.py`:

```python
from usan_agent.voicemail import VoicemailWatcher, build_matcher


def test_build_matcher_empty_uses_builtin():
    from usan_agent.voicemail import _PATTERN

    assert build_matcher([]) is _PATTERN


def test_build_matcher_custom_phrases_literal_and_case_insensitive():
    matcher = build_matcher(["please record your message"])
    assert matcher.search("PLEASE RECORD YOUR MESSAGE now")
    assert not matcher.search("you've reached the Smiths")  # built-in phrase NOT included


def test_build_matcher_escapes_regex_metachars():
    # A phrase with regex metachars must match literally, not as a pattern.
    matcher = build_matcher(["press 1 (now)"])
    assert matcher.search("please press 1 (now) to continue")


def test_build_matcher_blank_phrases_fall_back_to_builtin():
    from usan_agent.voicemail import _PATTERN

    assert build_matcher(["   ", ""]) is _PATTERN


def test_watcher_uses_injected_matcher():
    matcher = build_matcher(["custom greeting marker"])
    w = VoicemailWatcher(matcher=matcher)
    w.feed("this is a custom greeting marker hello")
    assert w.detected


def test_watcher_default_matcher_unchanged():
    w = VoicemailWatcher()
    w.feed("you've reached the Smiths, leave a message")
    assert w.detected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/agent && uv run pytest tests/test_voicemail.py -v`
Expected: FAIL — `build_matcher` missing; `VoicemailWatcher.__init__` takes no `matcher`.

- [ ] **Step 3: Edit `voicemail.py`**

Add `build_matcher` after `is_voicemail`, and update `VoicemailWatcher`:

```python
def build_matcher(trigger_phrases: list[str]) -> "re.Pattern[str]":
    """Compile a case-insensitive, LITERAL matcher from admin trigger phrases.

    Empty (or all-blank) phrases -> the built-in §7 _PATTERN. Phrases are re.escape'd
    and OR-joined so admin input is matched literally (never as a regex) — a false
    positive would hang up on a live elder.
    """
    cleaned = [p for p in trigger_phrases if p and p.strip()]
    if not cleaned:
        return _PATTERN
    return re.compile("|".join(re.escape(p) for p in cleaned), re.IGNORECASE)


class VoicemailWatcher:
    """Accumulate STT chunks and flag when a voicemail greeting is recognised."""

    def __init__(self, matcher: "re.Pattern[str] | None" = None) -> None:
        self._buffer = ""
        self._event = asyncio.Event()
        self._matcher = matcher or _PATTERN

    def feed(self, transcript: str) -> None:
        if self._event.is_set():
            return  # already detected; stop accumulating
        self._buffer = f"{self._buffer} {transcript}".strip()
        if self._matcher.search(self._buffer):
            self._event.set()

    @property
    def detected(self) -> bool:
        return self._event.is_set()

    async def wait_until_detected(self, window_s: float) -> bool:
        """True if a voicemail greeting is detected within `window_s` seconds."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=window_s)
            return True
        except TimeoutError:
            return False
```

> Keep the module-level `is_voicemail`/`_PATTERN` for back-compat (other tests use them).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_voicemail.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add services/agent/src/usan_agent/voicemail.py services/agent/tests/test_voicemail.py
git commit -m "feat(agent): per-call literal voicemail matcher from config trigger phrases"
```

---

## Task 8: `voicemail_action.py` — config-driven voicemail message

**Files:**
- Modify: `services/agent/src/usan_agent/voicemail_action.py`
- Test: `services/agent/tests/test_voicemail_action.py`

- [ ] **Step 1: Read the existing test file**

Read `services/agent/tests/test_voicemail_action.py` to match the existing stub style, then append
a test that passes the message explicitly:

```python
async def test_leave_voicemail_speaks_configured_message(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    from usan_agent import voicemail_action

    monkeypatch.setattr(voicemail_action, "report_voicemail_left", AsyncMock())
    ctx = MagicMock()
    ctx.delete_room = AsyncMock()
    ctx.shutdown = MagicMock()
    session = MagicMock()
    session.interrupt = MagicMock()
    handle = AsyncMock()()
    session.say = MagicMock(return_value=handle)

    await voicemail_action.leave_voicemail(
        ctx, session, "call-1", MagicMock(), voicemail_message="CUSTOM VM"
    )
    assert session.say.call_args.args[0] == "CUSTOM VM"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/agent && uv run pytest tests/test_voicemail_action.py -k configured -v`
Expected: FAIL — `leave_voicemail` takes no `voicemail_message`.

- [ ] **Step 3: Edit `voicemail_action.py`**

Remove the import-time binding of `VOICEMAIL_MESSAGE` and accept it as a keyword argument with a
default sourced from the agent config:

```python
"""The voicemail hangup sequence, extracted for unit testing.

cancel in-flight speech/LLM → speak the scripted leave-message → report the
outcome to the API → delete the room (hangs up the SIP leg) → shut the job down.
"""

from typing import Any

from loguru import logger

from usan_agent.agent_config import DEFAULT_AGENT_CONFIG
from usan_agent.api_client import report_voicemail_left
from usan_agent.settings import Settings


async def leave_voicemail(
    ctx: Any,
    session: Any,
    call_id: str | None,
    settings: Settings,
    voicemail_message: str = DEFAULT_AGENT_CONFIG.prompts.voicemail_message,
) -> None:
    log = logger.bind(call_id=call_id)
    log.info("Voicemail detected; leaving scripted message")

    session.interrupt(force=True)  # cancel the greeting / any in-flight reply
    handle = session.say(voicemail_message, allow_interruptions=False, add_to_chat_ctx=False)
    await handle  # wait for full playout before hanging up

    if call_id:
        await report_voicemail_left(call_id, settings)

    await ctx.delete_room()  # disconnects the SIP/PSTN leg = hangup
    ctx.shutdown(reason="voicemail_left")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_voicemail_action.py -v`
Expected: PASS (all — the existing tests call `leave_voicemail` with 4 positional args, which
still works via the defaulted 5th param).

- [ ] **Step 5: Commit**

```bash
git add services/agent/src/usan_agent/voicemail_action.py services/agent/tests/test_voicemail_action.py
git commit -m "feat(agent): leave_voicemail speaks the configured voicemail message"
```

---

## Task 9: `worker.py` — fetch config and thread it through every path

**Files:**
- Modify: `services/agent/src/usan_agent/worker.py`
- Test: `services/agent/tests/test_worker.py`

- [ ] **Step 1: Write/adjust the failing tests**

In `services/agent/tests/test_worker.py`:

(a) Add a fetch stub helper near the top:

```python
from usan_agent.agent_config import DEFAULT_AGENT_CONFIG


async def _fake_fetch(settings, *, direction, call_id=None):
    return DEFAULT_AGENT_CONFIG
```

(b) Update **every** `_fake_build_session` definition in this file to accept `cfg`:
`def _fake_build_session(settings, cfg=None, userdata=None):`.

(c) Update the builder stubs to the new signatures:
- `monkeypatch.setattr(worker, "build_check_in_agent", lambda cfg=None: MagicMock())`
- `monkeypatch.setattr(worker, "build_inbound_agent", lambda cfg, dv: MagicMock())`
- `_fake_build_check_in_agent()` → `_fake_build_check_in_agent(cfg=None)`
- `_fake_build_inbound_agent(dynamic_vars)` → `_fake_build_inbound_agent(cfg, dynamic_vars)` (read
  `dynamic_vars` from the 2nd param).

(d) In **every** `entrypoint`-driving test's monkeypatch block, add:
`monkeypatch.setattr(worker, "fetch_agent_config", _fake_fetch)` and, where not already patched,
`monkeypatch.setattr(worker, "register_metrics_flush", lambda *a, **k: None)`.

(e) Update the `_run_detection_window` direct-call tests + the `greet`/`leave_voicemail` stubs:
- `_fake_leave(ctx, session, call_id, settings, voicemail_message=None)`
- `_greet(_s, cfg=None, *, include_disclosure=True)`
- the two `worker._run_detection_window(...)` calls add `cfg=DEFAULT_AGENT_CONFIG` and (where a
  watcher is built for the human-falls-through test) it no longer references `VOICEMAIL_WINDOW_S`
  — instead the window comes from `cfg`; for that test pass a cfg with a tiny window:
  `cfg = DEFAULT_AGENT_CONFIG.model_copy(update={"voicemail_detection": DEFAULT_AGENT_CONFIG.voicemail_detection.model_copy(update={"window_s": 0.05})})`
  and assert no voicemail. (Remove the `monkeypatch.setattr(worker, "VOICEMAIL_WINDOW_S", 0.05)`
  line — that constant is no longer imported into `worker`.)

(f) Add a new test asserting the fetch is threaded and direction/call_id are passed:

```python
async def test_outbound_fetches_and_applies_config(monkeypatch):
    _settings(monkeypatch)
    seen = {}

    async def _fetch(settings, *, direction, call_id=None):
        seen["direction"] = direction
        seen["call_id"] = call_id
        return DEFAULT_AGENT_CONFIG

    def _fake_build_session(settings, cfg=None, userdata=None):
        seen["session_cfg"] = cfg
        session = MagicMock()
        session.start = AsyncMock()
        session.say = AsyncMock()
        session.on = MagicMock()
        return session

    monkeypatch.setattr(worker, "fetch_agent_config", _fetch)
    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "build_check_in_agent", lambda cfg=None: MagicMock())
    monkeypatch.setattr(worker, "register_transcript_flush", lambda *a, **k: None)
    monkeypatch.setattr(worker, "register_metrics_flush", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_run_detection_window", AsyncMock())
    monkeypatch.setattr(worker, "start_call_recording", AsyncMock())

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock()
    ctx.room.name = "usan-outbound-x"
    ctx.job.metadata = '{"call_id": "call-1", "direction": "outbound", "dynamic_vars": {}}'

    await worker.entrypoint(ctx)

    assert seen["direction"] == "outbound"
    assert seen["call_id"] == "call-1"
    assert seen["session_cfg"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/agent && uv run pytest tests/test_worker.py -v`
Expected: FAIL — `worker.fetch_agent_config` not imported; `cfg` not threaded.

- [ ] **Step 3: Edit `worker.py`**

(a) Update imports (add `AgentConfig`, `fetch_agent_config`, `build_matcher`; drop the
`RECORDING_DISCLOSURE`/`VOICEMAIL_WINDOW_S` imports now threaded via cfg):

```python
from usan_agent.agent_config import AgentConfig
from usan_agent.api_client import fetch_agent_config, start_inbound_call
from usan_agent.check_in import CheckInData, build_check_in_agent, build_inbound_agent
from usan_agent.ids import validate_call_id
from usan_agent.logging_config import configure_logging
from usan_agent.metrics_hooks import register_metrics_flush
from usan_agent.pipeline import build_agent, build_session, greet, say_recording_disclosure
from usan_agent.recording import start_call_recording
from usan_agent.settings import Settings, get_settings
from usan_agent.transcript import register_transcript_flush
from usan_agent.voicemail import VoicemailWatcher, build_matcher
from usan_agent.voicemail_action import leave_voicemail
```

(b) Delete the module-level `_INBOUND_OPENING` constant (now `cfg.prompts.inbound_opening`).

(c) Rewrite `_run_inbound`, `_run_detection_window`, and `entrypoint` to take + thread `cfg`:

```python
async def _run_inbound(ctx: JobContext, settings: Settings, cfg: AgentConfig, log: Any) -> None:
    """Inbound: wait for the caller, look them up, run a personalized check-in.

    Uses the inbound default config (cfg). No voicemail detection on inbound (spec §7).
    """
    participant = await ctx.wait_for_participant()
    phone = _caller_phone(participant)
    log.info("Inbound caller present (phone={phone})", phone=_mask_phone(phone))

    info = await start_inbound_call(phone, ctx.room.name, settings)
    if info and info.get("elder_known") and info.get("call_id"):
        call_id = str(info["call_id"])
        dynamic_vars = info.get("dynamic_vars") or {}
        data = CheckInData(
            call_id=call_id,
            settings=settings,
            job_ctx=ctx,
            goodbye_message=cfg.prompts.goodbye_message,
        )
        session = build_session(settings, cfg, userdata=data)
        agent = build_inbound_agent(cfg, dynamic_vars)
        register_transcript_flush(ctx, session, call_id, settings)
        register_metrics_flush(ctx, session, call_id, settings)
        await session.start(agent=agent, room=ctx.room)
        asyncio.create_task(_max_duration_guard(ctx, cfg.timing.max_call_duration_s))
        log.info("Inbound check-in started for known elder (call_id={cid})", cid=call_id)
        await say_recording_disclosure(session, cfg)
        await start_call_recording(ctx, call_id, settings)
        await session.generate_reply(instructions=cfg.prompts.inbound_opening)
        return

    # Unknown caller or lookup failed: greet-only, no per-elder state.
    session = build_session(settings, cfg)
    agent = build_agent(cfg)
    await session.start(agent=agent, room=ctx.room)
    log.info("Inbound greet-only (no known elder)")
    await greet(session, cfg)


async def _run_detection_window(
    ctx: JobContext,
    session: Any,
    watcher: VoicemailWatcher,
    *,
    call_id: str | None,
    settings: Settings,
    cfg: AgentConfig,
) -> None:
    """Greet, then over the detection window leave a voicemail or fall through."""
    await greet(session, cfg, include_disclosure=False)
    if await watcher.wait_until_detected(cfg.voicemail_detection.window_s):
        await leave_voicemail(
            ctx, session, call_id, settings, voicemail_message=cfg.prompts.voicemail_message
        )
```

> The `_max_duration_guard` referenced above is added in Task 10. To keep this task green on its
> own, add a temporary no-op stub now and replace it in Task 10 — OR implement Task 10's guard
> first. Recommended: implement the guard function (Task 10 Step 3) as part of this task's edit so
> the reference resolves, then Task 10 only adds the arming in `entrypoint` + the guard tests. If
> following tasks strictly in order, define `_max_duration_guard` here.

For clarity, define `_max_duration_guard` now (Task 10 adds its tests + the outbound arming):

```python
async def _max_duration_guard(ctx: JobContext, max_s: float) -> None:
    """Backstop: shut the job down if a call exceeds its configured max duration.

    A cost/safety cap (also covered API-side for outbound). Cancelled on normal call
    end, so a completed call never triggers a late shutdown.
    """
    try:
        await asyncio.sleep(max_s)
    except asyncio.CancelledError:
        return
    logger.bind(room=ctx.room.name).warning("Max call duration reached; ending job")
    ctx.shutdown(reason="max_call_duration")
```

(d) Rewrite `entrypoint` to fetch `cfg` after connect and thread it through outbound:

```python
async def entrypoint(ctx: JobContext) -> None:
    """Per-room entrypoint. LiveKit calls this once per dispatched job."""
    settings = get_settings()
    meta = parse_metadata(ctx.job.metadata)
    log = logger.bind(room=ctx.room.name, call_id=meta.call_id, direction=meta.direction)
    log.info("Job assigned, connecting to room")

    await ctx.connect()
    log.info("Connected to room")

    # Resolve the published agent config once per call (best-effort; never raises).
    cfg = await fetch_agent_config(settings, direction=meta.direction, call_id=meta.call_id)

    if meta.direction == "outbound" and meta.call_id:
        try:
            call_id = validate_call_id(meta.call_id)
        except ValueError:
            log.error("Invalid call_id in job metadata; refusing outbound job")
            ctx.shutdown(reason="invalid_metadata")
            return
        data = CheckInData(
            call_id=call_id,
            settings=settings,
            job_ctx=ctx,
            goodbye_message=cfg.prompts.goodbye_message,
        )
        session = build_session(settings, cfg, userdata=data)
        agent = build_check_in_agent(cfg)
        register_transcript_flush(ctx, session, call_id, settings)
        register_metrics_flush(ctx, session, call_id, settings)
        await session.start(agent=agent, room=ctx.room)
        asyncio.create_task(_max_duration_guard(ctx, cfg.timing.max_call_duration_s))
        log.info("Session started; waiting for participant")
        try:
            await asyncio.wait_for(
                ctx.wait_for_participant(), timeout=cfg.timing.answer_timeout_s
            )
        except TimeoutError:
            log.info("No participant within answer timeout; ending job")
            ctx.shutdown(reason="no_answer_timeout")
            return
        await say_recording_disclosure(session, cfg)
        await start_call_recording(ctx, call_id, settings)
        watcher = VoicemailWatcher(matcher=build_matcher(cfg.voicemail_detection.trigger_phrases))
        session.on("user_input_transcribed", lambda ev: watcher.feed(ev.transcript))
        log.info("Participant present; running voicemail detection window")
        await _run_detection_window(
            ctx, session, watcher, call_id=call_id, settings=settings, cfg=cfg
        )
        return

    # Inbound: caller already dialed in; no voicemail detection (spec §7).
    await _run_inbound(ctx, settings, cfg, log)
```

> Confirm no remaining reference to `RECORDING_DISCLOSURE`/`_INBOUND_OPENING`/`VOICEMAIL_WINDOW_S`
> in `worker.py`. `test_worker.py`/`test_recording_consent.py` import `RECORDING_DISCLOSURE` from
> `pipeline` (still exported there) — unaffected.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_worker.py tests/test_recording_consent.py -v`
Expected: PASS (all). Fix any remaining stub-signature mismatches surfaced by failures.

- [ ] **Step 5: Run the full agent suite**

Run: `cd services/agent && uv run pytest -q`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add services/agent/src/usan_agent/worker.py services/agent/tests/test_worker.py
git commit -m "feat(agent): fetch resolved config at call start and apply across all call paths"
```

---

## Task 10: Max-call-duration watchdog tests + outbound arming verification

> `_max_duration_guard` and its arming were added in Task 9 (so Task 9 stays green). This task adds
> the dedicated tests. If `_max_duration_guard` was NOT added in Task 9, add it now (see Task 9
> Step 3c) before writing these tests.

**Files:**
- Test: `services/agent/tests/test_worker.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `services/agent/tests/test_worker.py`:

```python
import asyncio


async def test_max_duration_guard_shuts_down_after_duration():
    ctx = MagicMock()
    ctx.shutdown = MagicMock()
    await worker._max_duration_guard(ctx, 0.01)  # tiny duration
    ctx.shutdown.assert_called_once()
    assert ctx.shutdown.call_args.kwargs.get("reason") == "max_call_duration"


async def test_max_duration_guard_noop_when_cancelled():
    ctx = MagicMock()
    ctx.shutdown = MagicMock()
    task = asyncio.create_task(worker._max_duration_guard(ctx, 100.0))
    await asyncio.sleep(0)  # let it start the sleep
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    ctx.shutdown.assert_not_called()  # cancelled before firing → no shutdown


async def test_outbound_arms_duration_guard(monkeypatch):
    _settings(monkeypatch)
    created = {}

    def _fake_create_task(coro):
        created["coro"] = coro
        coro.close()  # don't actually run the sleep
        return MagicMock()

    monkeypatch.setattr(worker.asyncio, "create_task", _fake_create_task)
    monkeypatch.setattr(worker, "fetch_agent_config", _fake_fetch)

    def _fake_build_session(settings, cfg=None, userdata=None):
        session = MagicMock()
        session.start = AsyncMock()
        session.say = AsyncMock()
        session.on = MagicMock()
        return session

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "build_check_in_agent", lambda cfg=None: MagicMock())
    monkeypatch.setattr(worker, "register_transcript_flush", lambda *a, **k: None)
    monkeypatch.setattr(worker, "register_metrics_flush", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_run_detection_window", AsyncMock())
    monkeypatch.setattr(worker, "start_call_recording", AsyncMock())

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock()
    ctx.room.name = "usan-outbound-x"
    ctx.job.metadata = '{"call_id": "call-1", "direction": "outbound", "dynamic_vars": {}}'

    await worker.entrypoint(ctx)
    assert "coro" in created  # the guard was armed
```

- [ ] **Step 2: Run tests to verify they pass (guard already exists from Task 9)**

Run: `cd services/agent && uv run pytest tests/test_worker.py -k duration -v`
Expected: PASS (3 tests). If `_max_duration_guard` is missing, add it (Task 9 Step 3c) and the
arming `asyncio.create_task(...)` lines, then re-run.

- [ ] **Step 3: Commit**

```bash
git add services/agent/tests/test_worker.py
git commit -m "test(agent): cover max-call-duration watchdog (fire, cancel, arm)"
```

---

## Task 11: Full verification (lint, types, suites) + design-spec sync

**Files:**
- Modify: `docs/superpowers/specs/2026-06-07-admin-ui-design.md` (mark P2 done; note resolve endpoint + worker-token decision)

- [ ] **Step 1: Lint + format both services**

Run:
```bash
cd apps/api && uv run ruff check . && uv run ruff format --check .
cd ../../services/agent && uv run ruff check . && uv run ruff format --check .
```
Expected: clean. Fix any findings.

- [ ] **Step 2: Type-check both services (CI runs mypy)**

Run:
```bash
cd apps/api && uv run mypy
cd ../../services/agent && uv run mypy
```
Expected: no errors. Watch the `dict[str, Any]` kwargs builders, `_TOOL_REGISTRY` typing, the
`# type: ignore[arg-type]` in `runtime.py`, and `re.Pattern[str]` annotations.

- [ ] **Step 3: Run both full test suites**

Run:
```bash
cd apps/api && uv run pytest -q
cd ../../services/agent && uv run pytest -q
```
Expected: all green.

- [ ] **Step 4: Update the design spec**

In `docs/superpowers/specs/2026-06-07-admin-ui-design.md`, update §6 (data flow) and §14 (phasing)
to reflect the shipped P2: a single `GET /v1/runtime/agent-config?direction=&call_id=` endpoint
guarded by the worker JWT, returning `{source, profile_id, version, config}` (always 200, default
fallback); the agent fetches once per call after `ctx.connect()` and degrades to its local
`DEFAULT_AGENT_CONFIG`; per-elder/per-call override resolution is wired (reads `Call.profile_override`
+ `Elder.agent_profile_id`) but the admin setters for those remain a later slice — the configurable
lever today is the direction default. Mark P2 complete.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-06-07-admin-ui-design.md
git commit -m "docs(admin-ui): sync design spec to shipped P2 agent integration"
```

---

## Self-Review Notes (author)

- **Spec coverage:** P2 = "agent fetches & applies the published config at call time." Covered by Tasks 1–10; resolution precedence (override→elder→default), publish-snapshot read, safe fallback, and every config field (prompts, voice, llm, stt, timing.answer_timeout + max_call_duration watchdog, tools enable, voicemail window/phrases/message, speech_advanced) are applied. Per-elder/per-call **setters** are intentionally deferred (the design's "per-elder assignment" slice); resolution still reads those columns so the setters light up automatically later.
- **No placeholders:** every code/test step has complete code; commands have expected output.
- **Type consistency:** `cfg` is `AgentConfig | None = None` everywhere (agent), `ResolvedAgentConfig` is the single response/repo type, `resolve_agent_config(*, profile_override, elder_profile_id, direction)` matches the router call, builder signatures (`build_session(settings, cfg, userdata)`, `build_inbound_agent(cfg, dynamic_vars)`, `greet(session, cfg, *, include_disclosure)`, `say_recording_disclosure(session, cfg)`, `leave_voicemail(..., voicemail_message)`) match their call sites and the updated stubs.
- **Green-at-every-task:** new params default to `DEFAULT_AGENT_CONFIG`; worker-test stub signatures are updated only in Task 9 where `worker.py` actually threads `cfg`. `_max_duration_guard` is defined in Task 9 (where it's referenced) and tested in Task 10.
- **Safety:** `end_call` force-included; voicemail phrases literal + per-call; no PHI/secrets logged; fetch best-effort with tight timeout and local-default fallback; watchdog generous default + cancel-on-end.
