"""Bridge the RetellAI agent + Retell-LLM contract onto native AgentProfile/Version (T035).

One ``AgentProfile`` IS one agent AND its response engine — ``agent_id`` and ``llm_id`` are
two prefixed views of the same row (data-model §5). The two-step RetellAI create flow maps
to one profile: ``create-retell-llm`` makes the profile (the response-engine half, left as an
unpublished draft); ``create-agent`` (which carries ``response_engine.llm_id``) binds the
agent half onto that same profile and publishes it, so it is immediately live.

Field mapping: ``general_prompt`` -> ``prompts.system_prompt``, ``begin_message`` ->
``prompts.greeting``, ``voice_id`` -> ``voice.cartesia_voice_id`` (via the alias map). The
requested ``model``/``model_temperature`` are echoed but NOT honored — the prompt runs on the
engine's own Vertex pipeline (PHI containment, Constitution II). Everything else the CRM sends
is preserved in a ``compat_extras`` blob inside ``draft_config`` and echoed back on read; that
key sits OUTSIDE the native ``AgentConfig`` schema, which is ``extra="ignore"``, so it never
disturbs native validation. Every overlay is validated through ``AgentConfig`` before persist,
so a malformed mapped value returns a clean 422 instead of 500-ing later on read.

Writes always PUBLISH (create-agent / update-* / publish-agent-version) so the profile is live
for the call path's ``is_live_profile`` precondition; ``create-retell-llm`` alone leaves a
draft. ``delete`` archives (the RetellAI "gone" — filtered out of get/list), never hard-deletes
config (kept for audit). The two repo errors that can surface — ``ProfileInUseError`` (archive
blocked) — map to ``CompatError(409)``; a missing/archived profile maps to 404.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api.compat import ids, voice_map
from usan_api.compat.errors import CompatError
from usan_api.compat.schemas.agents import (
    WEBHOOK_EVENTS,
    AgentListItemResponse,
    AgentResponse,
    CreateAgentRequest,
    PublishAgentVersionRequest,
    UpdateAgentRequest,
)
from usan_api.compat.schemas.retell_llm import (
    CreateRetellLlmRequest,
    LlmResponse,
    UpdateRetellLlmRequest,
)
from usan_api.compat.serialization import to_ms
from usan_api.db.base import ProfileStatus
from usan_api.db.models import AgentProfile, AgentProfileVersion
from usan_api.repositories import agent_profiles as agent_profiles_repo
from usan_api.repositories import compat_webhooks as compat_webhooks_repo
from usan_api.repositories import knowledge_bases as kb_repo
from usan_api.repositories.agent_profiles import ProfileInUseError
from usan_api.schemas.agent_config import DEFAULT_AGENT_CONFIG, AgentConfig
from usan_api.settings import Settings

# Actor attribution on the native repo writes (created_by/updated_by/published_by audit).
_ACTOR = "compat-api"
_EXTRAS_KEY = "compat_extras"


# --- config overlay helpers ------------------------------------------------------------
def _config_dict(profile: AgentProfile) -> dict[str, Any]:
    """A deep copy of the profile's draft config (never mutate the ORM-attached dict)."""
    return copy.deepcopy(profile.draft_config)


def _apply_llm_overlay(
    config: dict[str, Any],
    *,
    general_prompt: str | None,
    begin_message: str | None,
    knowledge_base_ids: list[str] | None = None,
) -> None:
    # ``model`` / ``model_temperature`` / ``s2s_model`` are intentionally NOT applied — the
    # prompt runs on the engine's own Vertex pipeline with engine-controlled sampling
    # (data-model §5, Constitution II). They are still echoed to the CRM via compat_extras,
    # just never honored.
    prompts = config["prompts"]
    if general_prompt is not None:
        prompts["system_prompt"] = general_prompt
    if begin_message is not None:
        prompts["greeting"] = begin_message
    # Phase 5b: knowledge_base_ids ARE honored — written into native config["llm"] so chat
    # generation reads cfg.llm.knowledge_base_ids. None = PATCH no-op (prior binding survives).
    if knowledge_base_ids is not None:
        config["llm"]["knowledge_base_ids"] = knowledge_base_ids


def _apply_voice_overlay(config: dict[str, Any], *, cartesia_voice_id: str) -> None:
    config["voice"]["cartesia_voice_id"] = cartesia_voice_id


def _merge_extras(config: dict[str, Any], half: str, payload: dict[str, Any]) -> None:
    """Merge the CRM's submitted fields into the echo blob (PATCH-friendly: prior fields
    survive). The blob is namespaced by half ('agent'/'llm') so one profile holds both views."""
    extras = config.setdefault(_EXTRAS_KEY, {})
    half_blob = dict(extras.get(half) or {})
    half_blob.update(payload)
    extras[half] = half_blob


def _validate_config(config: dict[str, Any]) -> None:
    """Fail fast with a clean 422 if an overlaid value violates the native AgentConfig schema
    (e.g. a begin_message over the greeting length cap), so reads never 500 later."""
    try:
        AgentConfig.model_validate(config)
    except Exception as exc:  # pydantic ValidationError (and any odd shape) -> documented 422
        raise CompatError(422, "invalid agent configuration") from exc


async def _validate_kb_ids(db: AsyncSession, kb_ids: list[str] | None) -> None:
    """Reject any knowledge_base_id that doesn't resolve within the caller's org (RLS). Cross-org
    is indistinguishable from absent -> a generic 422 that never acknowledges cross-org
    existence (the same id under another org simply returns None)."""
    for token in kb_ids or []:
        try:
            kb_uuid = ids.decode_kb_id(token)
        except CompatError as exc:
            raise CompatError(422, "unknown knowledge_base_id") from exc
        if await kb_repo.get_kb(db, kb_uuid) is None:
            raise CompatError(422, "unknown knowledge_base_id")


# --- name uniqueness (uq_agent_profiles_name_org) --------------------------------------
async def _unique_name(db: AsyncSession, base: str, *, exclude_id: uuid.UUID | None = None) -> str:
    """A name unique within the org (RetellAI agent names are NOT unique, but the native
    table is). Dedupe by suffixing; checks ALL profiles incl. archived (they hold the name)."""
    profiles = await agent_profiles_repo.list_profiles(db)
    taken = {p.name for p in profiles if p.id != exclude_id}
    if base not in taken:
        return base
    n = 2
    while f"{base} ({n})" in taken:
        n += 1
    return f"{base} ({n})"


def _provisional_llm_name() -> str:
    return f"retell-llm-{uuid.uuid4().hex[:8]}"


# --- profile loading -------------------------------------------------------------------
async def _load_active(
    db: AsyncSession, profile_id: uuid.UUID, *, kind: str, expected_channel: str | None = None
) -> AgentProfile:
    profile = await agent_profiles_repo.get_profile(db, profile_id)
    if profile is None or profile.status == ProfileStatus.ARCHIVED:
        raise CompatError(404, f"{kind} not found")
    # Cross-resource guard: agent_id/llm_id are two views of one UUID, so a chat row's id
    # re-prefixed as agent_<uuid> would otherwise resolve here. Agent ops pass 'voice'; chat
    # ops pass 'chat'; retell-llm ops pass None (an LLM is channel-agnostic shared infra).
    if expected_channel is not None and profile.channel != expected_channel:
        raise CompatError(404, f"{kind} not found")
    return profile


# --- webhook subscription seam (US2) ---------------------------------------------------
async def _register_webhook(
    db: AsyncSession,
    settings: Settings,
    profile_id: uuid.UUID,
    webhook_url: str,
    webhook_events: list[str] | None,
) -> str:
    events = webhook_events if webhook_events else list(WEBHOOK_EVENTS)
    bad = [e for e in events if e not in WEBHOOK_EVENTS]
    if bad:
        raise CompatError(422, f"unknown webhook event(s): {', '.join(bad)}")
    _endpoint, secret = await compat_webhooks_repo.register_subscription(
        db,
        settings,
        agent_profile_id=profile_id,
        webhook_url=webhook_url,
        webhook_events=events,
    )
    return secret


# --- create / update -------------------------------------------------------------------
async def _publish_and_commit(db: AsyncSession, profile_id: uuid.UUID, *, note: str) -> None:
    """Publish the current draft + commit, mapping a name-uniqueness violation
    (uq_agent_profiles_name_org) to a clean 409 — mirrors the native admin router. ``_unique_name``
    dedupes the common (sequential) case; this guards the residual TOCTOU window that only opens
    under true concurrency (the CRM is single-controller, so this is defense-in-depth)."""
    try:
        await agent_profiles_repo.publish(db, profile_id, note=note, actor_email=_ACTOR)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise CompatError(409, "agent name already exists") from exc


async def create_response_engine(
    db: AsyncSession, settings: Settings, body: CreateRetellLlmRequest
) -> AgentProfile:
    """create-retell-llm: a NEW profile (the response-engine half), left as an unpublished
    draft. ``create-agent`` referencing its llm_id binds the agent half and publishes it."""
    name = await _unique_name(db, _provisional_llm_name())
    await _validate_kb_ids(db, body.knowledge_base_ids)
    config = DEFAULT_AGENT_CONFIG.model_dump()
    _apply_llm_overlay(
        config,
        general_prompt=body.general_prompt,
        begin_message=body.begin_message,
        knowledge_base_ids=body.knowledge_base_ids,
    )
    _merge_extras(config, "llm", body.model_dump())
    _validate_config(config)
    # The provisional name is a uuid8 suffix (deduped), so create_profile's INSERT cannot
    # realistically collide — no IntegrityError guard needed on this path.
    profile = await agent_profiles_repo.create_profile(
        db, name=name, description=None, actor_email=_ACTOR
    )
    await agent_profiles_repo.update_draft(
        db, profile.id, config=config, description=None, actor_email=_ACTOR
    )
    await db.commit()
    await db.refresh(profile)
    return profile


async def bind_agent(
    db: AsyncSession, settings: Settings, body: CreateAgentRequest
) -> tuple[AgentProfile, str | None]:
    """create-agent: bind the agent half onto the profile its ``response_engine.llm_id``
    points at, then publish (immediately live). Returns (profile, one-time webhook secret)."""
    llm_id = body.response_engine.llm_id
    if not llm_id:
        raise CompatError(422, "response_engine.llm_id is required")
    profile = await _load_active(db, ids.decode_llm_id(llm_id), kind="response engine")
    if profile.published_version is not None and profile.channel != "voice":
        raise CompatError(409, "llm_id is already bound to a chat agent")
    cartesia = voice_map.resolve_voice_id(body.voice_id)

    config = _config_dict(profile)
    _apply_voice_overlay(config, cartesia_voice_id=cartesia)
    _merge_extras(config, "agent", body.model_dump())
    _validate_config(config)

    secret: str | None = None
    if body.webhook_url is not None:
        secret = await _register_webhook(
            db, settings, profile.id, body.webhook_url, body.webhook_events
        )

    updated = await agent_profiles_repo.update_draft(
        db, profile.id, config=config, description=None, actor_email=_ACTOR
    )
    if updated is None:  # pragma: no cover - loaded active above
        raise CompatError(404, "response engine not found")
    updated.channel = "voice"  # a bound voice agent is always channel='voice' (re-stamps a re-bind)
    if body.agent_name:
        # The pending rename is flushed (and the uniqueness constraint checked) by publish().
        updated.name = await _unique_name(db, body.agent_name, exclude_id=updated.id)
    await _publish_and_commit(db, profile.id, note="compat create-agent")
    await db.refresh(updated)
    return updated, secret


async def update_agent(
    db: AsyncSession, settings: Settings, agent_id: str, body: UpdateAgentRequest
) -> tuple[AgentProfile, str | None]:
    profile = await _load_active(
        db, ids.decode_agent_id(agent_id), kind="agent", expected_channel="voice"
    )
    config = _config_dict(profile)
    if body.voice_id is not None:
        _apply_voice_overlay(config, cartesia_voice_id=voice_map.resolve_voice_id(body.voice_id))
    _merge_extras(config, "agent", body.model_dump(exclude_none=True))
    _validate_config(config)

    secret: str | None = None
    if body.webhook_url is not None:
        secret = await _register_webhook(
            db, settings, profile.id, body.webhook_url, body.webhook_events
        )

    updated = await agent_profiles_repo.update_draft(
        db, profile.id, config=config, description=None, actor_email=_ACTOR
    )
    if updated is None:  # pragma: no cover
        raise CompatError(404, "agent not found")
    if body.agent_name:
        updated.name = await _unique_name(db, body.agent_name, exclude_id=updated.id)
    await _publish_and_commit(db, profile.id, note="compat update-agent")
    await db.refresh(updated)
    return updated, secret


async def update_response_engine(
    db: AsyncSession, settings: Settings, llm_id: str, body: UpdateRetellLlmRequest
) -> AgentProfile:
    profile = await _load_active(db, ids.decode_llm_id(llm_id), kind="response engine")
    await _validate_kb_ids(db, body.knowledge_base_ids)
    config = _config_dict(profile)
    _apply_llm_overlay(
        config,
        general_prompt=body.general_prompt,
        begin_message=body.begin_message,
        knowledge_base_ids=body.knowledge_base_ids,
    )
    _merge_extras(config, "llm", body.model_dump(exclude_none=True))
    _validate_config(config)
    await agent_profiles_repo.update_draft(
        db, profile.id, config=config, description=None, actor_email=_ACTOR
    )
    # No name change on this path, so the only uniqueness risk is the version index (native
    # single-controller assumption); reuse the guarded publish+commit for uniformity.
    await _publish_and_commit(db, profile.id, note="compat update-retell-llm")
    await db.refresh(profile)
    return profile


async def publish_agent_version(
    db: AsyncSession, agent_id: str, body: PublishAgentVersionRequest
) -> AgentProfile:
    """Publish the current draft as a new version. The requested body.version is advisory;
    native publish auto-assigns the next number — FROZEN (oracle): pinned by
    test_publish_returns_server_authoritative_version."""
    profile = await _load_active(
        db, ids.decode_agent_id(agent_id), kind="agent", expected_channel="voice"
    )
    note = body.version_title or "compat publish-agent-version"
    version = await agent_profiles_repo.publish(db, profile.id, note=note, actor_email=_ACTOR)
    if version is None:  # pragma: no cover
        raise CompatError(404, "agent not found")
    await db.commit()
    await db.refresh(profile)
    return profile


async def delete_agent_version(db: AsyncSession, agent_id: str, version: int) -> None:
    """Delete one historical agent version row, refusing (409) to delete the published one.

    The currently-published version number lives in ``profile.published_version`` (an int).
    AgentProfileVersion rows have no archived flag — hard delete is appropriate for
    historical version rows. The caller must not delete the currently-live version.
    """
    profile = await _load_active(
        db, ids.decode_agent_id(agent_id), kind="agent", expected_channel="voice"
    )
    if profile.published_version == version:
        raise CompatError(409, "cannot delete the currently published version")
    removed = await agent_profiles_repo.delete_version(db, profile.id, version)
    if removed is None:
        raise CompatError(404, "agent version not found")
    await db.commit()


async def delete_agent(db: AsyncSession, agent_id: str) -> None:
    """RetellAI delete == archive: the agent leaves the API view (get/list 404/omit) while the
    config is retained for audit. Hard delete is intentionally not exposed."""
    profile = await _load_active(
        db, ids.decode_agent_id(agent_id), kind="agent", expected_channel="voice"
    )
    try:
        archived = await agent_profiles_repo.archive_profile(db, profile.id)
    except ProfileInUseError as exc:
        raise CompatError(409, str(exc)) from exc
    if archived is None:  # pragma: no cover
        raise CompatError(404, "agent not found")
    await db.commit()


# --- reads -----------------------------------------------------------------------------
async def get_agent_profile(db: AsyncSession, agent_id: str) -> AgentProfile:
    return await _load_active(
        db, ids.decode_agent_id(agent_id), kind="agent", expected_channel="voice"
    )


async def get_llm_profile(db: AsyncSession, llm_id: str) -> AgentProfile:
    return await _load_active(db, ids.decode_llm_id(llm_id), kind="response engine")


async def list_agent_profiles(
    db: AsyncSession, *, channel: Literal["voice", "chat"] | None = None
) -> list[AgentProfile]:
    """The single agent inventory; archived (deleted) are excluded. ``channel`` filters voice vs
    chat (None = all, used by the channel-agnostic retell-llm list)."""
    profiles = await agent_profiles_repo.list_profiles(db, channel=channel)
    return [p for p in profiles if p.status != ProfileStatus.ARCHIVED]


async def list_agent_versions(
    db: AsyncSession, agent_id: str
) -> tuple[AgentProfile, list[AgentProfileVersion]]:
    """Return (profile, versions) so the router can re-use the profile for serialization."""
    profile = await _load_active(
        db, ids.decode_agent_id(agent_id), kind="agent", expected_channel="voice"
    )
    versions = await agent_profiles_repo.list_versions(db, profile.id)
    return profile, versions


# --- serialization ---------------------------------------------------------------------
def _version_fields(profile: AgentProfile) -> dict[str, Any]:
    return {
        "version": profile.published_version or 0,
        "is_published": profile.published_version is not None,
        "last_modification_timestamp": to_ms(profile.updated_at),
    }


def serialize_agent_version(
    profile: AgentProfile, version_row: AgentProfileVersion
) -> AgentResponse:
    """Full AgentResponse for one historical version entry.

    Builds the response from the *current* live profile config, overlaying the
    row's ``version`` number and ``published_at`` timestamp.  Historical per-version
    config snapshots are a known Phase-1 fidelity limit: the oracle requires the full
    AgentResponse shape for each entry, not a point-in-time config snapshot.
    """
    base = serialize_agent(profile)
    return base.model_copy(
        update={
            "version": version_row.version,
            "last_modification_timestamp": to_ms(version_row.published_at),
            "is_published": True,
        }
    )


def serialize_agent(profile: AgentProfile, *, webhook_secret: str | None = None) -> AgentResponse:
    config = profile.draft_config or {}
    extras = (config.get(_EXTRAS_KEY) or {}).get("agent") or {}
    cartesia = (config.get("voice") or {}).get("cartesia_voice_id")
    data: dict[str, Any] = dict(extras)  # echo the CRM's submitted config
    data.update(
        {
            "agent_id": ids.encode_agent_id(profile.id),
            "agent_name": profile.name,
            "response_engine": {"type": "retell-llm", "llm_id": ids.encode_llm_id(profile.id)},
            "voice_id": voice_map.to_retell_voice_id(cartesia),
            **_version_fields(profile),
        }
    )
    if webhook_secret is not None:
        # The dedicated signing secret is surfaced ONCE, only on the registering call.
        data["webhook_secret"] = webhook_secret
    return AgentResponse(**data)


def serialize_agent_list_item(profile: AgentProfile) -> AgentListItemResponse:
    """Smaller serialization for the POST /v2/list-agents paginated response.

    Returns the oracle-required AgentListItemResponse shape. ``channel`` is always
    "voice" (this engine is voice-only); ``tags`` is an empty dict (no native tag
    concept). ``user_modified_timestamp`` reuses the same ms-epoch source as
    ``last_modification_timestamp`` in serialize_agent (``to_ms(profile.updated_at)``).
    """
    return AgentListItemResponse(
        agent_id=ids.encode_agent_id(profile.id),
        agent_name=profile.name or "",
        channel="voice",
        user_modified_timestamp=to_ms(profile.updated_at) or 0,
        tags={},
    )


def serialize_llm(profile: AgentProfile, *, webhook_secret: str | None = None) -> LlmResponse:
    config = profile.draft_config or {}
    extras = (config.get(_EXTRAS_KEY) or {}).get("llm") or {}
    prompts = config.get("prompts") or {}
    data: dict[str, Any] = dict(extras)
    data.update(
        {
            "llm_id": ids.encode_llm_id(profile.id),
            "general_prompt": prompts.get("system_prompt"),
            "begin_message": prompts.get("greeting"),
            **_version_fields(profile),
        }
    )
    if webhook_secret is not None:
        data["webhook_secret"] = webhook_secret
    return LlmResponse(**data)
