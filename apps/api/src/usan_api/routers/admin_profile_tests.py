"""Pre-publish agent test endpoints (US5 / FR-025–FR-028, contract admin-api.md).

Two sandboxed test modes, both ADMIN-gated (viewers 403, FR-030), both PHI-safe:

- ``POST /v1/admin/profiles/{id}/test/llm`` — a text simulation. The draft prompt +
  synthetic ``sample_vars`` are sent to **Vertex AI via ADC** (``vertexai=True`` —
  NEVER the Gemini Developer API, Constitution II). Tools are presented to the model
  as SCHEMA-ONLY stubs derived from ``TOOL_CATALOG``; a tool call returns a canned
  synthetic string and is echoed to the UI — there is NO ``/v1/tools/*`` call and NO
  DB write (FR-027). The model→stub→continue loop is bounded (research R3).
- ``POST .../test/audio`` — mints a join-only short-TTL browser LiveKit token and
  dispatches the agent in ``session_kind="test"`` with the draft config + sample
  vars inline in metadata (see contracts/agent-test-session.md). No PSTN, no Call
  row, no phone number consumed (FR-028).

Each invocation emits exactly ONE structured PHI-free audit entry (actor email +
profile id + ``kind``) so live-provider test usage is observable — never the
sample-var values or message content (C1 / FR-029, Constitution VI).
"""

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import livekit_dispatch
from usan_api.admin_actor import get_actor_email
from usan_api.auth import get_tenant_db, require_admin_role, require_super_admin
from usan_api.db.base import AdminRole
from usan_api.prompt_substitution import build_vars, substitute
from usan_api.repositories import admin_audit
from usan_api.repositories import agent_profiles as repo
from usan_api.schemas.agent_config import AgentConfig, catalog_violations
from usan_api.schemas.profile_tests import (
    TestAudioRequest,
    TestAudioResponse,
    TestLlmRequest,
    TestLlmResponse,
    TestToolCall,
)
from usan_api.schemas.tool_catalog import TOOL_CATALOG
from usan_api.settings import Settings, get_settings
from usan_api.vertex_test import VertexTurn, run_vertex_turn

router = APIRouter(
    prefix="/v1/admin/profiles",
    tags=["admin-profile-tests"],
    dependencies=[Depends(require_super_admin)],
)

# Bound the model→stub-tool→continue loop so a misbehaving prompt cannot rack up
# unbounded Vertex calls in a single test invocation (research R3 iteration cap ~5).
_MAX_TOOL_ITERATIONS = 5

# The draft config + sample vars ride inside the LiveKit dispatch metadata for the audio
# test; LiveKit's CreateAgentDispatchRequest.metadata has an undocumented size limit
# (~64KB). Reject an oversized config with a clear 422 rather than failing opaquely at
# the gRPC layer.
_MAX_DISPATCH_METADATA_BYTES = 50_000

# A stub tool always returns this synthetic string — it is never the result of any
# real /v1/tools/* call (FR-027). The model sees a plausible-but-fake result so the
# conversation can continue without touching the database.
_STUB_TOOL_RESULT = "(test mode) tool executed successfully — no real action taken."


async def _resolve_draft_config(
    db: AsyncSession, profile_id: uuid.UUID, override: AgentConfig | None
) -> AgentConfig:
    """The config under test: an inline override, else the profile's stored draft.

    Re-validates the stored draft through ``AgentConfig`` on read (forward-compat
    invariant). 404 if the profile does not exist.
    """
    if override is not None:
        return override
    profile = await repo.get_profile(db, profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="profile not found")
    return AgentConfig.model_validate(profile.draft_config)


def _reject_off_catalog(cfg: AgentConfig) -> None:
    """Block a test run whose voice/model id is outside the curated catalogs (FR-014).

    Mirrors the handler-layer gate the persistence paths apply in update_draft/publish/
    rollback so a test exercises exactly the same allowlist a publishable config must —
    an unsupported id never reaches the live Vertex/Cartesia provider (security review
    PR #61, LOW #1). Runs before any provider call; the fabricated field-level ``loc``
    parses client-side like a pydantic 422.
    """
    violations = catalog_violations(cfg.model_dump())
    if violations:
        raise HTTPException(status_code=422, detail=violations)


# The module-level seam the tests patch. Kept as a thin indirection so the Vertex
# call (which needs ADC + a live project) is trivially mockable without a provider.
# It reads settings via get_settings() internally so the call site stays minimal.
async def _run_vertex_turn(
    *,
    model: str,
    temperature: float | None,
    system_instruction: str,
    tools: list[dict[str, object]],
    contents: list[dict[str, object]],
) -> VertexTurn:
    return await run_vertex_turn(
        model=model,
        temperature=temperature,
        system_instruction=system_instruction,
        tools=tools,
        contents=contents,
        settings=get_settings(),
    )


@router.post("/{profile_id}/test/llm", response_model=TestLlmResponse)
async def run_llm_test(
    profile_id: uuid.UUID,
    body: TestLlmRequest,
    db: AsyncSession = Depends(get_tenant_db),
    settings: Settings = Depends(get_settings),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> TestLlmResponse:
    # The text-test LLM path authenticates to Vertex via ADC and needs the GCP
    # project; without it the API cannot run the simulation (research R3 risk).
    if not settings.gcp_project:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="text test unavailable: GCP_PROJECT is not configured",
        )
    cfg = await _resolve_draft_config(db, profile_id, body.config)
    # FR-014 parity: reject an off-catalog voice/model BEFORE the Vertex call.
    _reject_off_catalog(cfg)

    # Substitute the synthetic sample vars into the system prompt exactly as the live
    # agent would (api-side parallel substitutor; no real contact PHI is loaded).
    values = build_vars({}, body.bounded_sample_vars(), timezone="", now=datetime.now(UTC))
    system_instruction = substitute(cfg.prompts.system_prompt, values)

    # Schema-only tool stubs from the catalog, filtered to the draft's enabled set.
    enabled = set(cfg.tools.enabled)
    tool_decls: list[dict[str, object]] = [
        {
            "name": spec.name,
            "description": spec.description,
            "parameters_json_schema": {"type": "object", "properties": {}},
        }
        for spec in TOOL_CATALOG
        if spec.name in enabled
    ]

    contents: list[dict[str, object]] = [
        {"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
        for m in body.messages
    ]

    echoed_tool_calls: list[TestToolCall] = []
    assistant_text = ""
    for _i in range(_MAX_TOOL_ITERATIONS):
        turn = await _run_vertex_turn(
            model=cfg.llm.model,
            temperature=cfg.llm.temperature,
            system_instruction=system_instruction,
            tools=tool_decls,
            contents=contents,
        )
        if turn.tool_calls:
            # Echo the requested tool calls; feed each a synthetic result and loop so
            # the model can continue. NEVER execute a real tool (no /v1/tools/* call).
            contents.append(
                {
                    "role": "model",
                    "parts": [
                        {"function_call": {"name": tc.name, "args": tc.args}}
                        for tc in turn.tool_calls
                    ],
                }
            )
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "function_response": {
                                "name": tc.name,
                                "response": {"result": _STUB_TOOL_RESULT},
                            }
                        }
                        for tc in turn.tool_calls
                    ],
                }
            )
            for tc in turn.tool_calls:
                echoed_tool_calls.append(TestToolCall(name=tc.name, args=tc.args))
            assistant_text = turn.text or assistant_text
            continue
        assistant_text = turn.text or assistant_text
        break

    # C1 / FR-029: one PHI-free audit entry — kind only, never sample-var values.
    await admin_audit.record(
        db,
        actor_email=actor,
        action="profile.test_llm",
        entity_type="agent_profile",
        entity_id=str(profile_id),
        detail={"kind": "test_llm"},
    )
    await db.commit()
    logger.bind(actor=actor, profile_id=str(profile_id), kind="test_llm").info(
        "Agent text test run"
    )
    return TestLlmResponse(assistant=assistant_text, tool_calls=echoed_tool_calls)


@router.post("/{profile_id}/test/audio", response_model=TestAudioResponse)
async def run_audio_test(
    profile_id: uuid.UUID,
    body: TestAudioRequest,
    db: AsyncSession = Depends(get_tenant_db),
    settings: Settings = Depends(get_settings),
    actor: str = Depends(get_actor_email),
    _: object = Depends(require_admin_role(AdminRole.ADMIN)),
) -> TestAudioResponse:
    cfg = await _resolve_draft_config(db, profile_id, body.config)
    # FR-014 parity: reject an off-catalog voice/model BEFORE minting a token/dispatch.
    _reject_off_catalog(cfg)

    # Reject an oversized config before dispatch: the draft config + sample vars are
    # embedded in the LiveKit dispatch metadata (size-limited); exceeding it would fail
    # opaquely at the gRPC layer. Admin-only path, so a 422 is the clear signal.
    metadata_bytes = len(
        json.dumps(
            {"test_config": cfg.model_dump(), "sample_vars": body.bounded_sample_vars()}
        ).encode("utf-8")
    )
    if metadata_bytes > _MAX_DISPATCH_METADATA_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="config is too large to run an audio test; shorten the prompt/template fields",
        )

    room = f"usan-test-{uuid.uuid4().hex}"
    identity = f"tester-{uuid.uuid4().hex[:8]}"
    token = livekit_dispatch.mint_browser_token(
        settings, room=room, identity=identity, name="Tester"
    )
    await livekit_dispatch.dispatch_test_agent(
        settings=settings,
        room=room,
        test_config=cfg.model_dump(),
        sample_vars=body.bounded_sample_vars(),
    )

    # C1 / FR-029: one PHI-free audit entry — kind + room only, never sample values.
    await admin_audit.record(
        db,
        actor_email=actor,
        action="profile.test_audio",
        entity_type="agent_profile",
        entity_id=str(profile_id),
        detail={"kind": "test_audio", "room": room},
    )
    await db.commit()
    logger.bind(actor=actor, profile_id=str(profile_id), kind="test_audio").info(
        "Agent audio test dispatched"
    )
    return TestAudioResponse(url=settings.livekit_url, token=token, room=room)
