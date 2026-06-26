import asyncio as _asyncio
import time
import uuid

import jwt
import pytest

from tests.conftest import counter_value as _counter_value
from usan_api import livekit_dispatch

# Operator bearer token for the management plane (matches conftest's OPERATOR_API_KEY).
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


def _create_contact(client, *, metadata: dict | None = None) -> str:
    r = client.post(
        "/v1/contacts",
        json={
            "name": "Ada",
            "phone_e164": f"+1555{str(uuid.uuid4().int)[:7]}",
            "timezone": "UTC",
            "metadata": metadata or {},
        },
        headers=_OP,
    )
    assert r.status_code == 201
    return r.json()["id"]


def _enqueue(client, contact_id: str) -> str:
    r = client.post(
        "/v1/calls",
        json={
            "contact_id": contact_id,
            "idempotency_key": f"tool-{uuid.uuid4()}",
            "dynamic_vars": {},
        },
        headers=_OP,
    )
    assert r.status_code == 202
    return r.json()["id"]


def _auth(call_id: str) -> dict:
    return {"Authorization": f"Bearer {_service_token(call_id)}"}


def test_log_wellness_ok(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": call_id, "mood": 4, "pain_level": 2, "notes": "feeling good"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert isinstance(r.json()["id"], int)


def test_log_wellness_requires_token(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": call_id, "mood": 3},
    )
    assert r.status_code == 401


def test_log_wellness_token_call_id_mismatch_403(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    wrong = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": call_id, "mood": 3},
        headers=_auth(wrong),
    )
    assert r.status_code == 403


def test_log_wellness_unknown_call_404(client, mock_dispatch):
    cid = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": cid, "mood": 3},
        headers=_auth(cid),
    )
    assert r.status_code == 404


def test_log_wellness_out_of_range_422(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/log_wellness",
        json={"call_id": call_id, "mood": 9},  # mood is 1..5
        headers=_auth(call_id),
    )
    assert r.status_code == 422


def test_log_medication_ok(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/log_medication",
        json={"call_id": call_id, "medication_name": "Aspirin", "taken": True},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert isinstance(r.json()["id"], int)


def test_log_medication_requires_token(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/log_medication",
        json={"call_id": call_id, "medication_name": "Aspirin", "taken": False},
    )
    assert r.status_code == 401


def test_log_medication_mismatch_403(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    wrong = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/log_medication",
        json={"call_id": call_id, "medication_name": "Aspirin", "taken": True},
        headers=_auth(wrong),
    )
    assert r.status_code == 403


_SCHEDULE = [
    {"name": "Aspirin", "dosage": "81mg", "times": ["08:00"]},
    {"name": "Metformin", "times": ["08:00", "20:00"]},
]


def test_get_today_meds_returns_schedule(client, mock_dispatch):
    contact_id = _create_contact(client, metadata={"medication_schedule": _SCHEDULE})
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/get_today_meds",
        json={"call_id": call_id},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    meds = r.json()["medications"]
    assert len(meds) == 2
    assert meds[0] == {"name": "Aspirin", "dosage": "81mg", "times": ["08:00"]}
    assert meds[1] == {"name": "Metformin", "dosage": None, "times": ["08:00", "20:00"]}


def test_get_today_meds_empty_when_no_schedule(client, mock_dispatch):
    contact_id = _create_contact(client)  # no medication_schedule in meta
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/get_today_meds",
        json={"call_id": call_id},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert r.json()["medications"] == []


def test_get_today_meds_skips_malformed_entries(client, mock_dispatch):
    bad = [{"name": "Good", "times": ["09:00"]}, {"dosage": "x"}, "nonsense"]
    contact_id = _create_contact(client, metadata={"medication_schedule": bad})
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/get_today_meds",
        json={"call_id": call_id},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    meds = r.json()["medications"]
    assert len(meds) == 1
    assert meds[0]["name"] == "Good"


def test_get_today_meds_mismatch_403(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/get_today_meds",
        json={"call_id": call_id},
        headers=_auth(str(uuid.uuid4())),
    )
    assert r.status_code == 403


def _answered_call(client, async_database_url) -> str:
    """Enqueue a call, then force it to IN_PROGRESS with a direct write."""
    import asyncio as _asyncio
    import uuid as _uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from usan_api.repositories import calls as calls_repo

    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)

    async def _answer() -> None:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                await calls_repo.mark_answered(db, _uuid.UUID(call_id), sip_call_id="SCL")
                await db.commit()
        finally:
            await engine.dispose()

    _asyncio.run(_answer())
    return call_id


def test_end_call_completes_in_progress(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    r = client.post(
        "/v1/tools/end_call",
        json={"call_id": call_id, "reason": "check_in_complete"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "completed"


def test_end_call_idempotent_noop(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    first = client.post(
        "/v1/tools/end_call",
        json={"call_id": call_id, "reason": "check_in_complete"},
        headers=_auth(call_id),
    )
    assert first.status_code == 200
    second = client.post(
        "/v1/tools/end_call",
        json={"call_id": call_id, "reason": "user_ended"},
        headers=_auth(call_id),
    )
    assert second.status_code == 200
    assert second.json()["status"] == "completed"  # unchanged; reason not overwritten


def test_end_call_mismatch_403(client, mock_dispatch, async_database_url):
    call_id = _answered_call(client, async_database_url)
    r = client.post(
        "/v1/tools/end_call",
        json={"call_id": call_id, "reason": "x"},
        headers=_auth(str(uuid.uuid4())),
    )
    assert r.status_code == 403


def test_end_call_unknown_call_404(client, mock_dispatch):
    cid = str(uuid.uuid4())
    r = client.post(
        "/v1/tools/end_call",
        json={"call_id": cid, "reason": "x"},
        headers=_auth(cid),
    )
    assert r.status_code == 404


def test_end_call_noop_when_not_in_progress(client, mock_dispatch):
    # A call that hasn't been answered (status "dialing") is not completed by end_call;
    # the handler returns the call's current status unchanged.
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/end_call",
        json={"call_id": call_id, "reason": "x"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "dialing"


def test_log_transcript_inserts_segments(client, mock_dispatch, async_database_url):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/log_transcript",
        json={
            "call_id": call_id,
            "segments": [
                {"role": "assistant", "content": "Hello!", "started_at": "2026-06-01T12:00:00Z"},
                {"role": "user", "content": "I'm good", "started_at": "2026-06-01T12:00:05Z"},
                {
                    "role": "tool",
                    "content": "log_wellness",
                    "tool_name": "log_wellness",
                    "tool_args": {"mood": 4},
                    "started_at": "2026-06-01T12:00:06Z",
                },
            ],
        },
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert r.json()["count"] == 3


def test_log_transcript_requires_token(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/log_transcript",
        json={
            "call_id": call_id,
            "segments": [{"role": "user", "content": "hi", "started_at": "2026-06-01T12:00:00Z"}],
        },
    )
    assert r.status_code == 401


def test_log_transcript_mismatch_403(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/log_transcript",
        json={
            "call_id": call_id,
            "segments": [{"role": "user", "content": "hi", "started_at": "2026-06-01T12:00:00Z"}],
        },
        headers=_auth(str(uuid.uuid4())),
    )
    assert r.status_code == 403


def test_log_transcript_empty_segments_422(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/log_transcript",
        json={"call_id": call_id, "segments": []},
        headers=_auth(call_id),
    )
    assert r.status_code == 422


def test_flag_for_followup_ok(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
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
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={"call_id": call_id, "severity": "routine", "category": "other", "reason": "x"},
    )
    assert r.status_code == 401


def test_flag_for_followup_call_id_mismatch_403(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
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
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={"call_id": call_id, "severity": "panic", "category": "medical", "reason": "x"},
        headers=_auth(call_id),
    )
    assert r.status_code == 422


def test_flag_for_followup_increments_metric(client, mock_dispatch):
    from usan_api.observability.custom_metrics import FOLLOWUP_FLAGS_TOTAL

    before = _counter_value(FOLLOWUP_FLAGS_TOTAL, severity="urgent", category="safety")
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/flag_for_followup",
        json={"call_id": call_id, "severity": "urgent", "category": "safety", "reason": "fell"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    after = _counter_value(FOLLOWUP_FLAGS_TOTAL, severity="urgent", category="safety")
    assert after == before + 1


# --- send_sms -------------------------------------------------------------
def _publish_sms_profile(async_database_url, *, body="Hello {{first_name}} from USAN."):
    """Create a default-outbound profile whose draft enables send_sms with one template,
    publish it, set it default-outbound. Uses the REAL repo API (publish / set_default)."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from usan_api.repositories import agent_profiles as profiles_repo
    from usan_api.schemas.agent_config import AgentConfig, SmsTemplate, SmsToolConfig

    async def _do():
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                profile = await profiles_repo.create_profile(
                    db, name=f"sms-{uuid.uuid4()}", description=None, actor_email="op@x.io"
                )
                # Build the draft through the real Pydantic schema (immutable copy) rather
                # than spreading raw JSONB dicts, mirroring the codebase's no-mutation rule.
                base = AgentConfig.model_validate(profile.draft_config)
                enabled = base.tools.enabled
                if "send_sms" not in enabled:
                    enabled = [*enabled, "send_sms"]
                tools = base.tools.model_copy(
                    update={
                        "enabled": enabled,
                        "sms": SmsToolConfig(
                            templates=[SmsTemplate(key="greet", label="Greet", body=body)]
                        ),
                    }
                )
                draft = base.model_copy(update={"tools": tools}).model_dump()
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
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    # mark the call answered/in_progress is not required for enqueue; the endpoint
    # only needs the call + contact to exist (mirrors log_wellness, which works at QUEUED).
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
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/send_sms",
        json={"call_id": call_id, "template_key": "nope"},
        headers=_auth(call_id),
    )
    assert r.status_code == 404


def test_send_sms_no_sms_config_404(client, mock_dispatch):
    # Fresh-deployment path: no default-outbound profile configures send_sms, so
    # resolution falls back to DEFAULT_AGENT_CONFIG where tools.sms is None.
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/send_sms",
        json={"call_id": call_id, "template_key": "greet"},
        headers=_auth(call_id),
    )
    assert r.status_code == 404


def test_send_sms_requires_token(client, mock_dispatch):
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post("/v1/tools/send_sms", json={"call_id": call_id, "template_key": "greet"})
    assert r.status_code == 401


def test_send_sms_mismatch_403(client, mock_dispatch, async_database_url):
    # Service token bound to a different call_id must be rejected by _authorize_call.
    _publish_sms_profile(async_database_url)
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/send_sms",
        json={"call_id": call_id, "template_key": "greet"},
        headers=_auth(str(uuid.uuid4())),
    )
    assert r.status_code == 403


def _publish_override_profile(async_database_url, *, template_key, body="Hi from override."):
    """Publish a profile enabling send_sms with one template under `template_key`, and
    return its id. NOT marked default — used as a per-call profile_override."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from usan_api.repositories import agent_profiles as profiles_repo
    from usan_api.schemas.agent_config import AgentConfig, SmsTemplate, SmsToolConfig

    async def _do():
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                profile = await profiles_repo.create_profile(
                    db, name=f"ovr-{uuid.uuid4()}", description=None, actor_email="op@x.io"
                )
                base = AgentConfig.model_validate(profile.draft_config)
                enabled = base.tools.enabled
                if "send_sms" not in enabled:
                    enabled = [*enabled, "send_sms"]
                tools = base.tools.model_copy(
                    update={
                        "enabled": enabled,
                        "sms": SmsToolConfig(
                            templates=[SmsTemplate(key=template_key, label="Ovr", body=body)]
                        ),
                    }
                )
                draft = base.model_copy(update={"tools": tools}).model_dump()
                await profiles_repo.update_draft(
                    db, profile.id, config=draft, description=None, actor_email="op@x.io"
                )
                await profiles_repo.publish(db, profile.id, note=None, actor_email="op@x.io")
                await db.commit()
                return profile.id
        finally:
            await engine.dispose()

    return asyncio.run(_do())


def _set_call_override(async_database_url, call_id, profile_id):
    """Point a call's profile_override at `profile_id` via a direct write."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from usan_api.repositories import calls as calls_repo

    async def _do():
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                call = await calls_repo.get_call(db, uuid.UUID(call_id))
                call.profile_override = profile_id
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(_do())


@pytest.mark.skip(
    reason="Contact.phone_e164 is NOT NULL (and the contact-create API requires it), so a "
    "null/empty phone cannot be seeded through the helpers. The 409 guard in send_sms is "
    "cheap defense for any future path that could persist a blank number."
)
def test_send_sms_missing_phone_409(client, mock_dispatch, async_database_url):
    _publish_sms_profile(async_database_url)
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    r = client.post(
        "/v1/tools/send_sms",
        json={"call_id": call_id, "template_key": "greet"},
        headers=_auth(call_id),
    )
    assert r.status_code == 409


def test_send_sms_resolves_profile_override_over_direction_default(
    client, mock_dispatch, async_database_url
):
    # The direction-default profile offers only "greet"; the call's profile_override
    # offers only "ovr_greet". The override is top of the precedence walk, so the
    # override's template resolves and the direction-default's "greet" does NOT.
    _publish_sms_profile(async_database_url)  # default-outbound, template "greet"
    override_id = _publish_override_profile(async_database_url, template_key="ovr_greet")
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    _set_call_override(async_database_url, call_id, override_id)

    # Override-only template succeeds (override tier won).
    ok = client.post(
        "/v1/tools/send_sms",
        json={"call_id": call_id, "template_key": "ovr_greet"},
        headers=_auth(call_id),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["status"] == "pending"

    # The direction-default's "greet" is invisible once the override resolves -> 404.
    miss = client.post(
        "/v1/tools/send_sms",
        json={"call_id": call_id, "template_key": "greet"},
        headers=_auth(call_id),
    )
    assert miss.status_code == 404


def test_send_sms_per_call_cap_409(client, mock_dispatch, async_database_url):
    # A confused/hijacked LLM must not queue unbounded carrier traffic: send_sms is
    # exempt from the global rate limiter (in-call tool path), so the endpoint
    # itself refuses once MAX_SMS_PER_CALL rows exist for the call — any status,
    # since already-sent texts count toward the contact's per-call budget too.
    from usan_api.routers import tools as tools_router

    # Pin the literal value: raising the per-call budget must be a deliberate,
    # reviewed decision (carrier traffic to an contact's phone), not a constant tweak.
    assert tools_router.MAX_SMS_PER_CALL == 3

    _publish_sms_profile(async_database_url)
    contact_id = _create_contact(client)
    call_id = _enqueue(client, contact_id)
    for _ in range(tools_router.MAX_SMS_PER_CALL):
        r = client.post(
            "/v1/tools/send_sms",
            json={"call_id": call_id, "template_key": "greet"},
            headers=_auth(call_id),
        )
        assert r.status_code == 200, r.text

    # The budget counts ALL statuses: flushing a queued row to `sent` must not
    # refund the cap (a pending-only count would reset after every flush).
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from usan_api.repositories import sms_messages as sms_repo

    async def _mark_one_sent():
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                rows = await sms_repo.list_messages(db, limit=100)
                row = next(r for r in rows if str(r.call_id) == call_id)
                await sms_repo.mark_sent(db, row.id, telnyx_message_id="m-cap")
                await db.commit()
        finally:
            await engine.dispose()

    asyncio.run(_mark_one_sent())
    r = client.post(
        "/v1/tools/send_sms",
        json={"call_id": call_id, "template_key": "greet"},
        headers=_auth(call_id),
    )
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# CALLS_TOTAL: web calls must not inflate the inbound direction bucket
# ---------------------------------------------------------------------------


def _make_answered_web_call(async_database_url: str) -> str:
    """Insert a WEB_CALL directly and force it to IN_PROGRESS."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from usan_api.db.base import CallDirection, CallStatus, CallType
    from usan_api.db.models import Call

    async def _run() -> str:
        engine = create_async_engine(async_database_url, poolclass=NullPool)
        try:
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                call = Call(
                    call_type=CallType.WEB_CALL,
                    status=CallStatus.IN_PROGRESS,
                    direction=CallDirection.INBOUND,  # placeholder
                    livekit_room=f"usan-web-{uuid.uuid4().hex}",
                    contact_id=None,
                    answered_at=__import__("datetime").datetime.now(
                        __import__("datetime").timezone.utc
                    ),
                )
                db.add(call)
                await db.flush()
                call_id = str(call.id)
                await db.commit()
        finally:
            await engine.dispose()
        return call_id

    return _asyncio.run(_run())


def test_end_call_web_call_does_not_increment_calls_total(client, async_database_url):
    """A web call reaching end_call must NOT increment CALLS_TOTAL.

    direction=INBOUND is only a placeholder for WEB_CALL rows. Counting it
    would inflate the inbound bucket and misrepresent phone-call volume.
    Phone calls (regression path) continue to be counted as before.
    """
    from usan_api.observability.custom_metrics import CALLS_TOTAL

    # Snapshot before.
    before_inbound = _counter_value(CALLS_TOTAL, direction="inbound", end_reason="completed")

    web_call_id = _make_answered_web_call(async_database_url)
    r = client.post(
        "/v1/tools/end_call",
        json={"call_id": web_call_id, "reason": "check_in_complete"},
        headers=_auth(web_call_id),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "completed"

    # Counter must not have moved.
    after_inbound = _counter_value(CALLS_TOTAL, direction="inbound", end_reason="completed")
    assert after_inbound == before_inbound, (
        "CALLS_TOTAL inbound bucket incremented for a WEB_CALL — "
        "web calls must be excluded from direction-keyed phone metrics"
    )


def test_end_call_phone_call_still_increments_calls_total(
    client, mock_dispatch, async_database_url
):
    """Phone calls (regression): end_call must still increment CALLS_TOTAL."""
    from usan_api.observability.custom_metrics import CALLS_TOTAL

    call_id = _answered_call(client, async_database_url)
    before = _counter_value(CALLS_TOTAL, direction="outbound", end_reason="completed")

    r = client.post(
        "/v1/tools/end_call",
        json={"call_id": call_id, "reason": "check_in_complete"},
        headers=_auth(call_id),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "completed"

    after = _counter_value(CALLS_TOTAL, direction="outbound", end_reason="completed")
    assert after == before + 1, (
        "CALLS_TOTAL outbound bucket must still be incremented for phone calls"
    )
