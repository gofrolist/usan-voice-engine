# Plan 4b — Egress & Recording Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Scope note.** Plan 4b adds **call recording** only. The deployment baseline (GCP Compute Engine VM, Caddy TLS, Secret Manager prod `.env`, tag-driven deploy) is Plan 4a and is assumed done. Transcript capture is Plan 3c and is **not** touched here. This plan does **not** add multi-tenant auth on the recording URL, or per-elder consent configuration (deferred).

**Goal:** Every call's mixed audio is recorded by LiveKit Egress and written to a Google Cloud Storage bucket; `apps/api` learns the object location via webhook, stores it on the call, and serves a short-lived signed URL on demand — keyless (no service-account key files).

**Architecture:** The agent starts an audio-only **RoomComposite egress** at session start (uniform across inbound + outbound), writing `recordings/{YYYY-MM-DD}/{call_id}.ogg` to GCS. A new `livekit-egress` container performs the upload using the VM's Application Default Credentials. LiveKit posts `egress_started` / `egress_ended` to the existing `/webhooks/livekit` endpoint (one URL receives all event types); the handler stores `calls.egress_id` then `calls.recording_uri`. `GET /v1/calls/{id}` signs a V4 URL via IAM `signBlob` (self-impersonation — no key on disk).

**Tech Stack:** FastAPI + SQLAlchemy async (Python 3.14, uv) · LiveKit Agents 1.5.x + `livekit-api` (Python 3.12, uv) · `google-cloud-storage` (keyless V4 signing) · `livekit/egress:v1.13.0` · Terraform (google provider) · Docker Compose · Alembic · pytest (`asyncio_mode = "auto"`).

---

## Context for the implementer

### Decisions locked (from the design conversation, recorded in spec §9)
1. **Egress trigger:** agent-side, at session start. One helper, called on both the outbound and the inbound-known paths.
2. **GCS auth: keyless.** Egress writes via ADC (the VM's attached service account). The API signs read URLs via IAM `signBlob` (self-impersonation). No key files.
3. **Tracking is minimal:** `calls.recording_uri` + a new `calls.egress_id`. No separate egress-status table.
4. **Consent:** a static recording-disclosure line is spoken at the start of every call. Per-elder configuration is deferred.

### Verified external facts (do not re-derive)
- **LiveKit egress request** (`from livekit import api`, livekit-api 1.x; field names stable across 1.x):
  `api.RoomCompositeEgressRequest(room_name=..., audio_only=True, file_outputs=[api.EncodedFileOutput(file_type=api.EncodedFileType.OGG, filepath=..., gcp=api.GCPUpload(bucket=..., credentials=""))])`, started with `await lkapi.egress.start_room_composite_egress(req)` → `EgressInfo` with `.egress_id`. Use the **repeated** `file_outputs=[...]` (not the deprecated singular `file=`). `credentials=""` ⇒ the egress worker uses ADC. RoomComposite egress **auto-stops when the room closes**, so the agent never needs to stop it (the agent already `delete_room()`s on hangup).
- **Egress webhook** arrives at the SAME `/webhooks/livekit` URL. `api.WebhookReceiver` parses it; access fields snake_case: `event.event` ∈ {`egress_started`,`egress_updated`,`egress_ended`}, `event.egress_info.egress_id` / `.room_name` / `.status` / `.file_results[0].filename` / `.file_results[0].location`. Completion is signalled by `event.egress_info.status == api.EgressStatus.EGRESS_COMPLETE`; treat any other terminal status as a failed recording.
- **Keyless GCS V4 signing footgun:** on GCE, `google.auth.default()` returns credentials whose `service_account_email` is the literal `"default"` and whose `.token` is `None` **until you call `credentials.refresh(Request())`**. Always refresh before signing. Passing `service_account_email=` + `access_token=` to `Blob.generate_signed_url(version="v4", ...)` routes signing through IAM `signBlob` (no key).
- **Egress container** (`livekit/egress:v1.13.0`): configured inline via `EGRESS_CONFIG_BODY`; needs only `api_key`/`api_secret`/`ws_url`/`redis.address`/`insecure: true` (ws:// internal hop). It runs headless Chrome even for audio-only, so it requires `cap_add: [SYS_ADMIN]` and `shm_size: 1gb`. No storage credentials in the container — the bucket is per-request and ADC handles the upload.

### House conventions to follow
- Commit format `type(scope): description`, scopes `infra`/`api`/`agent`/`docs`. **No** `Co-Authored-By` trailer (matches existing history).
- Settings: `pydantic_settings.BaseSettings`, `Field(..., alias="ENV_VAR")`, optional via `Field(default=...)`. `get_settings()` is `lru_cache`d.
- LiveKit clients: `async with api.LiveKitAPI(...) as lkapi:` over the **http(s)** URL (convert `ws://`→`http://`).
- Webhook side effects: change-in-function, **commit in the handler**; never raise on best-effort work.
- Repo functions are `async def f(db: AsyncSession, ...) -> Model | None`; correlate calls by `livekit_room` (room names are uuid4; take the most recent).
- Tests: sync test functions using `asyncio.run(...)` for DB work (api), or `async def` without a marker for agent collaborators (auto mode). DB tests request the `client` fixture (spins the pg container + `alembic upgrade head`, truncates on teardown).
- Per-task gate: `ruff check . && ruff format .` then `uv run mypy src`, then commit (pre-commit also runs ruff/gitleaks).

---

## Deployment prerequisites (operator — before going live, NOT before coding)

The code tasks (1–12) are fully testable without GCP. Before the first production deploy that records:

1. `cd infra/terraform && terraform apply` — creates the recordings bucket, grants the VM SA `roles/storage.objectAdmin` on it + `roles/iam.serviceAccountTokenCreator` on itself, and enables `iamcredentials.googleapis.com`. Note the `recordings_bucket` output.
2. In the `usan-prod-env` Secret Manager secret, set `GCS_BUCKET=<that bucket name>` (and optionally `RECORDING_SIGNED_URL_TTL_S`).
3. Confirm the VM instance access scope is `cloud-platform` (it is — `main.tf` sets `scopes = ["cloud-platform"]`), otherwise `signBlob` returns 403.
4. Redeploy the stack (the new `livekit-egress` container ships with it).

---

## File Structure

**apps/api**
- Create `src/usan_api/object_storage.py` — keyless V4 signed-URL generation (`generate_signed_url`, `_parse_gs_uri`).
- Create `migrations/versions/0005_call_egress_id.py` — adds `calls.egress_id`.
- Modify `src/usan_api/settings.py` — add `gcs_bucket`, `recording_signed_url_ttl_s`.
- Modify `src/usan_api/db/models.py` — add `Call.egress_id`.
- Modify `src/usan_api/schemas/call.py` — `CallResponse` gains `egress_id` + `presigned_recording_url`; `from_model` gains a keyword arg.
- Modify `src/usan_api/repositories/calls.py` — `_latest_by_room`, `set_egress_id`, `set_recording_uri`; refactor `mark_completed_if_in_progress` onto the helper.
- Modify `src/usan_api/routers/webhooks.py` — `_recording_uri` helper + egress event dispatch.
- Modify `src/usan_api/routers/calls.py` — `_presigned_recording_url` + sign on `GET /v1/calls/{id}`.
- Modify `pyproject.toml` — add `google-cloud-storage`.
- Tests: `tests/test_recording_model.py`, `tests/test_object_storage.py`, `tests/test_recording_repo.py`, `tests/test_recording_url.py`; extend `tests/test_settings.py`, `tests/test_webhooks.py`.

**services/agent**
- Create `src/usan_agent/recording.py` — `start_call_recording`, `recording_filepath`, `_http_url`.
- Modify `src/usan_agent/settings.py` — add `gcs_bucket`.
- Modify `src/usan_agent/pipeline.py` — `RECORDING_DISCLOSURE` + `greet()` speaks it.
- Modify `src/usan_agent/worker.py` — start recording on both check-in paths; speak disclosure on inbound-known.
- Modify `pyproject.toml` — declare `livekit-api` explicitly.
- Tests: `tests/test_recording.py`, `tests/test_recording_consent.py`; extend `tests/test_worker.py`.

**infra**
- Create `terraform/storage.tf` — bucket + IAM + API enablement.
- Modify `terraform/variables.tf`, `terraform/outputs.tf`, `terraform/terraform.tfvars.example`.
- Modify `docker-compose.yml` — add the `egress` service.
- Modify `.env.example`, `.env.prod.example` — recording vars.
- Modify `README.md` — recording runbook note.

---

## Task 1: DB column `calls.egress_id` (model + migration)

**Files:**
- Modify: `apps/api/src/usan_api/db/models.py`
- Create: `apps/api/migrations/versions/0005_call_egress_id.py`
- Test: `apps/api/tests/test_recording_model.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_recording_model.py`:

```python
from usan_api.db.models import Call


def test_call_model_has_egress_id_column():
    assert "egress_id" in Call.__table__.columns
    assert Call.__table__.columns["egress_id"].nullable is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_recording_model.py -v`
Expected: FAIL — `KeyError: 'egress_id'`.

- [ ] **Step 3: Add the column to the model**

In `apps/api/src/usan_api/db/models.py`, add the line immediately after the `recording_uri` mapped column:

```python
    recording_uri: Mapped[str | None] = mapped_column(Text)
    egress_id: Mapped[str | None] = mapped_column(Text)
```

- [ ] **Step 4: Create the migration**

Create `apps/api/migrations/versions/0005_call_egress_id.py`:

```python
"""add calls.egress_id for recording egress correlation

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-02

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE calls ADD COLUMN egress_id TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE calls DROP COLUMN IF EXISTS egress_id")
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd apps/api && uv run pytest tests/test_recording_model.py -v`
Expected: PASS. (The migration is exercised end-to-end by Task 4's DB tests.)

- [ ] **Step 6: Lint, type-check, commit**

```bash
cd apps/api && ruff check . && ruff format . && uv run mypy src
git add apps/api/src/usan_api/db/models.py apps/api/migrations/versions/0005_call_egress_id.py apps/api/tests/test_recording_model.py
git commit -m "feat(api): add calls.egress_id column + migration for recording"
```

---

## Task 2: API settings + dependency

**Files:**
- Modify: `apps/api/src/usan_api/settings.py`
- Modify: `apps/api/pyproject.toml`
- Test: `apps/api/tests/test_settings.py`

- [ ] **Step 1: Write the failing test**

Append to `apps/api/tests/test_settings.py`:

```python
def test_recording_settings_defaults(monkeypatch):
    for k, v in {
        "DATABASE_URL": "postgresql://u:p@h:5432/d",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "JWT_SIGNING_KEY": "s" * 32,
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    from usan_api.settings import Settings

    s = Settings()
    assert s.gcs_bucket is None
    assert s.recording_signed_url_ttl_s == 3600


def test_recording_settings_from_env(monkeypatch):
    for k, v in {
        "DATABASE_URL": "postgresql://u:p@h:5432/d",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "a" * 32,
        "LIVEKIT_URL": "ws://livekit:7880",
        "JWT_SIGNING_KEY": "s" * 32,
        "GCS_BUCKET": "usan-rec",
        "RECORDING_SIGNED_URL_TTL_S": "600",
    }.items():
        monkeypatch.setenv(k, v)
    from usan_api.settings import Settings

    s = Settings()
    assert s.gcs_bucket == "usan-rec"
    assert s.recording_signed_url_ttl_s == 600
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_settings.py -k recording -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'gcs_bucket'`.

- [ ] **Step 3: Add the settings fields**

In `apps/api/src/usan_api/settings.py`, add inside `class Settings` (after `jwt_signing_key`):

```python
    gcs_bucket: str | None = Field(default=None, alias="GCS_BUCKET")
    recording_signed_url_ttl_s: int = Field(
        default=3600, ge=60, le=604800, alias="RECORDING_SIGNED_URL_TTL_S"
    )
```

- [ ] **Step 4: Add the dependency**

In `apps/api/pyproject.toml`, add to the `dependencies` array:

```toml
    "google-cloud-storage>=2.18.0",
```

Then resolve it: `cd apps/api && uv sync`.

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd apps/api && uv run pytest tests/test_settings.py -k recording -v`
Expected: PASS.

- [ ] **Step 6: Lint, type-check, commit**

```bash
cd apps/api && ruff check . && ruff format . && uv run mypy src
git add apps/api/src/usan_api/settings.py apps/api/pyproject.toml apps/api/uv.lock apps/api/tests/test_settings.py
git commit -m "feat(api): add GCS recording settings + google-cloud-storage dep"
```

---

## Task 3: `object_storage` — keyless V4 signed URLs

**Files:**
- Create: `apps/api/src/usan_api/object_storage.py`
- Test: `apps/api/tests/test_object_storage.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_object_storage.py`:

```python
import datetime
from types import SimpleNamespace

import pytest

from usan_api import object_storage


def test_parse_gs_uri_ok():
    assert object_storage._parse_gs_uri("gs://b/recordings/2026-06-02/x.ogg") == (
        "b",
        "recordings/2026-06-02/x.ogg",
    )


@pytest.mark.parametrize("bad", ["http://b/x", "gs://b", "gs://b/", "gs:///x", "gs://"])
def test_parse_gs_uri_rejects_malformed(bad):
    with pytest.raises(ValueError):
        object_storage._parse_gs_uri(bad)


def test_generate_signed_url_keyless(monkeypatch):
    captured: dict = {}
    creds = SimpleNamespace(
        service_account_email="sa@example.iam.gserviceaccount.com",
        token="ya29.token",
        refresh=lambda request: None,
    )
    monkeypatch.setattr(
        object_storage.google.auth, "default", lambda scopes=None: (creds, "proj")
    )

    class _Blob:
        def generate_signed_url(self, **kwargs):
            captured.update(kwargs)
            return "https://signed.example/url"

    class _Bucket:
        def blob(self, name):
            captured["blob_name"] = name
            return _Blob()

    class _Client:
        def __init__(self, credentials=None):
            captured["credentials"] = credentials

        def bucket(self, name):
            captured["bucket"] = name
            return _Bucket()

    monkeypatch.setattr(object_storage.storage, "Client", _Client)

    url = object_storage.generate_signed_url("gs://b/recordings/2026-06-02/x.ogg", 3600)

    assert url == "https://signed.example/url"
    assert captured["bucket"] == "b"
    assert captured["blob_name"] == "recordings/2026-06-02/x.ogg"
    assert captured["version"] == "v4"
    assert captured["method"] == "GET"
    assert captured["expiration"] == datetime.timedelta(seconds=3600)
    assert captured["service_account_email"] == "sa@example.iam.gserviceaccount.com"
    assert captured["access_token"] == "ya29.token"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_object_storage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_api.object_storage'`.

- [ ] **Step 3: Write the module**

Create `apps/api/src/usan_api/object_storage.py`:

```python
"""Keyless V4 signed GET URLs for GCS recordings.

Signs via IAM signBlob using the runtime's attached service account (ADC) — the SA
self-impersonates, so no private-key file is needed. On a GCE VM the SA must hold
roles/iam.serviceAccountTokenCreator on itself and read access to the object.
Blocking (a network call to IAM signBlob); call via asyncio.to_thread.
"""

import datetime

import google.auth
import google.auth.transport.requests
from google.cloud import storage


def _parse_gs_uri(uri: str) -> tuple[str, str]:
    """Split gs://bucket/object/key into (bucket, key). Raises ValueError if malformed."""
    if not uri.startswith("gs://"):
        raise ValueError(f"not a gs:// URI: {uri!r}")
    bucket, _, key = uri[len("gs://") :].partition("/")
    if not bucket or not key:
        raise ValueError(f"gs:// URI missing bucket or object key: {uri!r}")
    return bucket, key


def generate_signed_url(gs_uri: str, ttl_seconds: int) -> str:
    """Return a V4 signed GET URL for a gs:// object, signed keylessly via IAM signBlob."""
    bucket_name, blob_name = _parse_gs_uri(gs_uri)

    # ADC. On a GCE VM this is the attached service account via the metadata server.
    credentials, _project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    # MUST refresh: pre-refresh, service_account_email is the literal "default" and
    # token is None — signing would fail or sign as the wrong principal.
    credentials.refresh(google.auth.transport.requests.Request())
    sa_email = credentials.service_account_email  # type: ignore[attr-defined]

    client = storage.Client(credentials=credentials)
    blob = client.bucket(bucket_name).blob(blob_name)
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(seconds=ttl_seconds),
        method="GET",
        service_account_email=sa_email,
        access_token=credentials.token,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd apps/api && uv run pytest tests/test_object_storage.py -v`
Expected: PASS.

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && ruff check . && ruff format . && uv run mypy src
git add apps/api/src/usan_api/object_storage.py apps/api/tests/test_object_storage.py
git commit -m "feat(api): keyless V4 signed-URL generation for GCS recordings"
```

---

## Task 4: Repository — `set_egress_id`, `set_recording_uri`

**Files:**
- Modify: `apps/api/src/usan_api/repositories/calls.py`
- Test: `apps/api/tests/test_recording_repo.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_recording_repo.py`:

```python
import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo


async def _seed(async_database_url: str, room: str) -> uuid.UUID:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    try:
        async with factory() as db:
            elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
            call = await calls_repo.create_call(
                db,
                elder_id=elder.id,
                direction=CallDirection.OUTBOUND,
                status=CallStatus.IN_PROGRESS,
                livekit_room=room,
            )
            await db.commit()
            return call.id
    finally:
        await engine.dispose()


async def _apply(async_database_url: str, room: str, *, egress_id=None, recording_uri=None):
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            if egress_id is not None:
                call = await calls_repo.set_egress_id(db, room, egress_id)
            else:
                call = await calls_repo.set_recording_uri(db, room, recording_uri)
            await db.commit()
            return call
    finally:
        await engine.dispose()


def test_set_egress_id_persists(client, async_database_url):
    room = "usan-outbound-eg1"
    asyncio.run(_seed(async_database_url, room))
    call = asyncio.run(_apply(async_database_url, room, egress_id="EG_1"))
    assert call is not None and call.egress_id == "EG_1"


def test_set_recording_uri_persists(client, async_database_url):
    room = "usan-outbound-rc1"
    asyncio.run(_seed(async_database_url, room))
    call = asyncio.run(_apply(async_database_url, room, recording_uri="gs://b/x.ogg"))
    assert call is not None and call.recording_uri == "gs://b/x.ogg"


def test_set_egress_id_unknown_room_returns_none(client, async_database_url):
    call = asyncio.run(_apply(async_database_url, "no-such-room", egress_id="EG_x"))
    assert call is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_recording_repo.py -v`
Expected: FAIL — `AttributeError: module 'usan_api.repositories.calls' has no attribute 'set_egress_id'`.

- [ ] **Step 3: Add the helper + setters; refactor `mark_completed_if_in_progress`**

In `apps/api/src/usan_api/repositories/calls.py`, add a private helper and the two setters (place them next to `mark_completed_if_in_progress`):

```python
async def _latest_by_room(db: AsyncSession, livekit_room: str) -> Call | None:
    # Room names are uuid4 so a collision is astronomically unlikely; take the most
    # recent match rather than scalar_one_or_none(), which would 500 on a duplicate.
    result = await db.execute(
        select(Call)
        .where(Call.livekit_room == livekit_room)
        .order_by(Call.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def set_egress_id(db: AsyncSession, livekit_room: str, egress_id: str) -> Call | None:
    call = await _latest_by_room(db, livekit_room)
    if call is None:
        return None
    call.egress_id = egress_id
    await db.flush()
    await db.refresh(call)
    return call


async def set_recording_uri(db: AsyncSession, livekit_room: str, recording_uri: str) -> Call | None:
    call = await _latest_by_room(db, livekit_room)
    if call is None:
        return None
    call.recording_uri = recording_uri
    await db.flush()
    await db.refresh(call)
    return call
```

Then refactor the existing `mark_completed_if_in_progress` to reuse the helper (behaviour-preserving — replace the inline `select(...)` block with the helper call):

```python
async def mark_completed_if_in_progress(db: AsyncSession, livekit_room: str) -> Call | None:
    call = await _latest_by_room(db, livekit_room)
    if call is None or call.status is not CallStatus.IN_PROGRESS:
        return None
    call.status = CallStatus.COMPLETED
    call.ended_at = _utcnow()
    call.end_reason = "hangup"
    if call.answered_at is not None:
        call.duration_seconds = int((call.ended_at - call.answered_at).total_seconds())
    await db.flush()
    await db.refresh(call)
    return call
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_recording_repo.py tests/test_webhooks.py -v`
Expected: PASS (the existing webhook tests confirm the `mark_completed_if_in_progress` refactor is behaviour-preserving).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && ruff check . && ruff format . && uv run mypy src
git add apps/api/src/usan_api/repositories/calls.py apps/api/tests/test_recording_repo.py
git commit -m "feat(api): repo setters for egress_id and recording_uri"
```

---

## Task 5: Webhook — handle `egress_started` / `egress_ended`

**Files:**
- Modify: `apps/api/src/usan_api/routers/webhooks.py`
- Test: `apps/api/tests/test_webhooks.py`

- [ ] **Step 1: Write the failing unit tests for the URI helper**

Append to `apps/api/tests/test_webhooks.py`:

```python
from types import SimpleNamespace

from livekit import api

from usan_api.routers.webhooks import _recording_uri


def _egress_info(status, files):
    return SimpleNamespace(status=status, file_results=files)


def test_recording_uri_complete_with_bucket():
    info = _egress_info(
        api.EgressStatus.EGRESS_COMPLETE,
        [SimpleNamespace(filename="recordings/2026-06-02/x.ogg", location="gs://orig/x")],
    )
    assert _recording_uri(info, "bkt") == "gs://bkt/recordings/2026-06-02/x.ogg"


def test_recording_uri_complete_without_bucket_uses_location():
    info = _egress_info(
        api.EgressStatus.EGRESS_COMPLETE,
        [SimpleNamespace(filename="recordings/x.ogg", location="gs://orig/x.ogg")],
    )
    assert _recording_uri(info, None) == "gs://orig/x.ogg"


def test_recording_uri_failed_returns_none():
    info = _egress_info(
        api.EgressStatus.EGRESS_FAILED,
        [SimpleNamespace(filename="x", location="y")],
    )
    assert _recording_uri(info, "bkt") is None


def test_recording_uri_no_files_returns_none():
    info = _egress_info(api.EgressStatus.EGRESS_COMPLETE, [])
    assert _recording_uri(info, "bkt") is None
```

Also append a webhook-payload helper and integration tests (reuse the file's existing `_sign`, `_seed_call`, and `CallStatus` import; add a DB read helper):

```python
from sqlalchemy.ext.asyncio import async_sessionmaker as _asm
from sqlalchemy.ext.asyncio import create_async_engine as _cae
from sqlalchemy.pool import NullPool as _NullPool

from usan_api.repositories import calls as _calls_repo


def _egress_event(event, room, *, egress_id="EG1", status="EGRESS_COMPLETE",
                  filename="recordings/2026-06-02/x.ogg",
                  location="gs://b/recordings/2026-06-02/x.ogg"):
    info = {"egressId": egress_id, "roomName": room, "status": status}
    if event == "egress_ended":
        info["fileResults"] = [{"filename": filename, "location": location}]
    return json.dumps({"event": event, "egressInfo": info, "id": "ev1", "createdAt": 1})


async def _read_call(async_database_url, call_id):
    engine = _cae(async_database_url, poolclass=_NullPool)
    factory = _asm(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            return await _calls_repo.get_call(db, call_id)
    finally:
        await engine.dispose()


def test_livekit_webhook_egress_started_sets_egress_id(client, async_database_url):
    room = "usan-outbound-egs"
    call_id = asyncio.run(
        _seed_call(async_database_url, room, status=CallStatus.IN_PROGRESS, answered=True)
    )
    body = _egress_event("egress_started", room, egress_id="EG_42")
    token = _sign(body, "key", "a" * 32)
    r = client.post("/webhooks/livekit", content=body, headers={"Authorization": token})
    assert r.status_code == 200
    call = asyncio.run(_read_call(async_database_url, call_id))
    assert call.egress_id == "EG_42"


def test_livekit_webhook_egress_ended_stores_recording_uri(client, async_database_url):
    room = "usan-outbound-ege"
    call_id = asyncio.run(
        _seed_call(async_database_url, room, status=CallStatus.IN_PROGRESS, answered=True)
    )
    # GCS_BUCKET is unset in the test env, so the handler falls back to fileResults.location.
    body = _egress_event("egress_ended", room, location="gs://b/recordings/2026-06-02/x.ogg")
    token = _sign(body, "key", "a" * 32)
    r = client.post("/webhooks/livekit", content=body, headers={"Authorization": token})
    assert r.status_code == 200
    call = asyncio.run(_read_call(async_database_url, call_id))
    assert call.recording_uri == "gs://b/recordings/2026-06-02/x.ogg"


def test_livekit_webhook_egress_ended_failed_stores_no_recording(client, async_database_url):
    room = "usan-outbound-egf"
    call_id = asyncio.run(
        _seed_call(async_database_url, room, status=CallStatus.IN_PROGRESS, answered=True)
    )
    body = _egress_event("egress_ended", room, status="EGRESS_FAILED")
    token = _sign(body, "key", "a" * 32)
    r = client.post("/webhooks/livekit", content=body, headers={"Authorization": token})
    assert r.status_code == 200
    call = asyncio.run(_read_call(async_database_url, call_id))
    assert call.recording_uri is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/api && uv run pytest tests/test_webhooks.py -k "egress or recording_uri" -v`
Expected: FAIL — `ImportError: cannot import name '_recording_uri'`.

- [ ] **Step 3: Implement the helper + handler dispatch**

In `apps/api/src/usan_api/routers/webhooks.py`, update the imports at the top:

```python
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from livekit import api
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import livekit_webhooks
from usan_api.db.session import get_db
from usan_api.repositories import calls as calls_repo
from usan_api.settings import Settings, get_settings
```

Add the helper (module-level, after `_ROOM_END_EVENTS`):

```python
def _recording_uri(info: Any, gcs_bucket: str | None) -> str | None:
    """The gs:// URI for a completed egress, or None if it produced no usable file."""
    if info.status != api.EgressStatus.EGRESS_COMPLETE or not info.file_results:
        return None
    object_key = info.file_results[0].filename
    if gcs_bucket and object_key:
        return f"gs://{gcs_bucket}/{object_key}"
    return info.file_results[0].location or None
```

Replace the body of `livekit_webhook` after the verification block (keep the verification and the `return {"ok": True}`):

```python
    if event.event in _ROOM_END_EVENTS and event.room and event.room.name:
        call = await calls_repo.mark_completed_if_in_progress(db, event.room.name)
        if call is not None:
            await db.commit()
            logger.bind(call_id=str(call.id), room=event.room.name).info(
                "Call completed via room_finished webhook"
            )
    elif event.event == "egress_started" and event.egress_info.room_name:
        info = event.egress_info
        call = await calls_repo.set_egress_id(db, info.room_name, info.egress_id)
        if call is not None:
            await db.commit()
            logger.bind(call_id=str(call.id), egress_id=info.egress_id).info(
                "Recorded egress_id via egress_started webhook"
            )
    elif event.event == "egress_ended" and event.egress_info.room_name:
        info = event.egress_info
        uri = _recording_uri(info, settings.gcs_bucket)
        if uri is None:
            logger.bind(room=info.room_name, status=int(info.status)).warning(
                "Egress ended without a usable recording"
            )
        else:
            call = await calls_repo.set_recording_uri(db, info.room_name, uri)
            if call is not None:
                await db.commit()
                logger.bind(call_id=str(call.id), recording_uri=uri).info(
                    "Stored recording_uri via egress_ended webhook"
                )
    return {"ok": True}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_webhooks.py -v`
Expected: PASS (all existing + new).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd apps/api && ruff check . && ruff format . && uv run mypy src
git add apps/api/src/usan_api/routers/webhooks.py apps/api/tests/test_webhooks.py
git commit -m "feat(api): handle egress_started/egress_ended webhooks"
```

---

## Task 6: `GET /v1/calls/{id}` returns a signed recording URL

**Files:**
- Modify: `apps/api/src/usan_api/schemas/call.py`
- Modify: `apps/api/src/usan_api/routers/calls.py`
- Test: `apps/api/tests/test_recording_url.py`

- [ ] **Step 1: Write the failing test**

Create `apps/api/tests/test_recording_url.py`:

```python
import asyncio
import uuid

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from usan_api import object_storage
from usan_api.db.base import CallDirection, CallStatus
from usan_api.repositories import calls as calls_repo
from usan_api.repositories import elders as elders_repo
from usan_api.settings import get_settings


async def _seed(async_database_url: str, room: str, *, recording_uri=None) -> uuid.UUID:
    engine = create_async_engine(async_database_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    phone = f"+1555{str(uuid.uuid4().int)[:7]}"
    try:
        async with factory() as db:
            elder = await elders_repo.create_elder(db, name="A", phone_e164=phone, timezone="UTC")
            call = await calls_repo.create_call(
                db,
                elder_id=elder.id,
                direction=CallDirection.OUTBOUND,
                status=CallStatus.COMPLETED,
                livekit_room=room,
            )
            if recording_uri is not None:
                await calls_repo.set_recording_uri(db, room, recording_uri)
            await db.commit()
            return call.id
    finally:
        await engine.dispose()


def test_get_call_without_recording_has_no_presigned_url(client, async_database_url):
    call_id = asyncio.run(_seed(async_database_url, "usan-outbound-norec"))
    body = client.get(f"/v1/calls/{call_id}").json()
    assert body["recording_uri"] is None
    assert body["presigned_recording_url"] is None
    assert body["egress_id"] is None


def test_get_call_with_recording_returns_signed_url(client, async_database_url, monkeypatch):
    uri = "gs://test-bucket/recordings/2026-06-02/x.ogg"
    call_id = asyncio.run(_seed(async_database_url, "usan-outbound-sign1", recording_uri=uri))
    monkeypatch.setenv("GCS_BUCKET", "test-bucket")
    get_settings.cache_clear()
    monkeypatch.setattr(
        object_storage, "generate_signed_url", lambda gs_uri, ttl: f"https://signed.example/{gs_uri}"
    )
    body = client.get(f"/v1/calls/{call_id}").json()
    assert body["recording_uri"] == uri
    assert body["presigned_recording_url"] == f"https://signed.example/{uri}"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/api && uv run pytest tests/test_recording_url.py -v`
Expected: FAIL — `KeyError: 'presigned_recording_url'`.

- [ ] **Step 3: Extend `CallResponse`**

In `apps/api/src/usan_api/schemas/call.py`, replace the `CallResponse` class with:

```python
class CallResponse(BaseModel):
    id: uuid.UUID
    elder_id: uuid.UUID | None
    direction: str
    status: str
    idempotency_key: str | None
    livekit_room: str | None
    attempt: int
    recording_uri: str | None
    egress_id: str | None
    presigned_recording_url: str | None
    created_at: datetime

    @classmethod
    def from_model(
        cls, call: Call, *, presigned_recording_url: str | None = None
    ) -> CallResponse:
        return cls(
            id=call.id,
            elder_id=call.elder_id,
            direction=call.direction.value,
            status=call.status.value,
            idempotency_key=call.idempotency_key,
            livekit_room=call.livekit_room,
            attempt=call.attempt,
            recording_uri=call.recording_uri,
            egress_id=call.egress_id,
            presigned_recording_url=presigned_recording_url,
            created_at=call.created_at,
        )
```

- [ ] **Step 4: Sign on read in the GET handler**

In `apps/api/src/usan_api/routers/calls.py`, add `import asyncio` at the top and add `object_storage` to the package import:

```python
import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from usan_api import dialer, livekit_dispatch, object_storage
```

Add a helper (module-level, near the other private helpers) and replace `get_call`:

```python
async def _presigned_recording_url(call: Call, settings: Settings) -> str | None:
    """Sign a short-lived GET URL for the call's recording, or None if absent/disabled."""
    if not call.recording_uri or not settings.gcs_bucket:
        return None
    try:
        url = await asyncio.to_thread(
            object_storage.generate_signed_url,
            call.recording_uri,
            settings.recording_signed_url_ttl_s,
        )
    except Exception:
        logger.bind(call_id=str(call.id)).warning("Failed to sign recording URL")
        return None
    # Access log: every issued recording URL is recorded (spec §10).
    logger.bind(call_id=str(call.id), recording_uri=call.recording_uri).info(
        "Recording URL accessed"
    )
    return url


@router.get("/{call_id}", response_model=CallResponse)
async def get_call(
    call_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CallResponse:
    call = await calls_repo.get_call(db, call_id)
    if call is None:
        raise HTTPException(status_code=404, detail="call not found")
    presigned = await _presigned_recording_url(call, settings)
    return CallResponse.from_model(call, presigned_recording_url=presigned)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd apps/api && uv run pytest tests/test_recording_url.py tests/test_calls.py -v`
Expected: PASS (existing `test_calls.py` still green — `from_model`'s new arg defaults to `None`).

- [ ] **Step 6: Lint, type-check, commit**

```bash
cd apps/api && ruff check . && ruff format . && uv run mypy src
git add apps/api/src/usan_api/schemas/call.py apps/api/src/usan_api/routers/calls.py apps/api/tests/test_recording_url.py
git commit -m "feat(api): return a signed recording URL from GET /v1/calls/{id}"
```

---

## Task 7: Agent — `recording` module + setting

**Files:**
- Modify: `services/agent/src/usan_agent/settings.py`
- Create: `services/agent/src/usan_agent/recording.py`
- Modify: `services/agent/pyproject.toml`
- Test: `services/agent/tests/test_recording.py`

- [ ] **Step 1: Write the failing test**

Create `services/agent/tests/test_recording.py`:

```python
import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from usan_agent import recording
from usan_agent.settings import Settings

_BASE_ENV = {
    "LIVEKIT_API_KEY": "key",
    "LIVEKIT_API_SECRET": "a" * 32,
    "LIVEKIT_URL": "ws://livekit:7880",
    "CARTESIA_API_KEY": "c",
    "GEMINI_API_KEY": "g",
    "DEFAULT_CARTESIA_VOICE_ID": "v",
    "API_BASE_URL": "http://api:8000",
    "JWT_SIGNING_KEY": "s" * 32,
}


@pytest.fixture
def settings_with_bucket(monkeypatch):
    for k, v in {**_BASE_ENV, "GCS_BUCKET": "usan-rec"}.items():
        monkeypatch.setenv(k, v)
    return Settings()


@pytest.fixture
def settings_no_bucket(monkeypatch):
    for k, v in _BASE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GCS_BUCKET", raising=False)
    return Settings()


def test_recording_filepath_format():
    fixed = datetime.datetime(2026, 6, 2, 15, 0, tzinfo=datetime.UTC)
    assert recording.recording_filepath("abc-123", now=fixed) == "recordings/2026-06-02/abc-123.ogg"


def test_http_url_converts_ws_scheme():
    assert recording._http_url("ws://livekit:7880") == "http://livekit:7880"
    assert recording._http_url("wss://lk.example") == "https://lk.example"


async def test_recording_disabled_without_bucket(monkeypatch, settings_no_bucket):
    made = MagicMock()
    monkeypatch.setattr(recording.api, "LiveKitAPI", made)
    ctx = SimpleNamespace(room=SimpleNamespace(name="room-1"))
    result = await recording.start_call_recording(ctx, "call-1", settings_no_bucket)
    assert result is None
    made.assert_not_called()


async def test_start_call_recording_builds_audio_only_ogg_request(monkeypatch, settings_with_bucket):
    egress = MagicMock()
    egress.start_room_composite_egress = AsyncMock(return_value=SimpleNamespace(egress_id="EG_7"))
    lkapi = MagicMock()
    lkapi.egress = egress
    lkapi.__aenter__ = AsyncMock(return_value=lkapi)
    lkapi.__aexit__ = AsyncMock(return_value=False)
    captured: dict = {}

    def _factory(url, api_key, api_secret):
        captured["url"] = url
        return lkapi

    monkeypatch.setattr(recording.api, "LiveKitAPI", _factory)
    ctx = SimpleNamespace(room=SimpleNamespace(name="room-9"))

    result = await recording.start_call_recording(ctx, "call-9", settings_with_bucket)

    assert result == "EG_7"
    assert captured["url"] == "http://livekit:7880"
    req = egress.start_room_composite_egress.await_args.args[0]
    assert req.room_name == "room-9"
    assert req.audio_only is True
    assert len(req.file_outputs) == 1
    out = req.file_outputs[0]
    assert out.file_type == recording.api.EncodedFileType.OGG
    assert out.filepath.startswith("recordings/")
    assert out.filepath.endswith("/call-9.ogg")
    assert out.gcp.bucket == "usan-rec"
    assert out.gcp.credentials == ""


async def test_start_call_recording_best_effort_on_error(monkeypatch, settings_with_bucket):
    egress = MagicMock()
    egress.start_room_composite_egress = AsyncMock(side_effect=RuntimeError("boom"))
    lkapi = MagicMock()
    lkapi.egress = egress
    lkapi.__aenter__ = AsyncMock(return_value=lkapi)
    lkapi.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(recording.api, "LiveKitAPI", lambda **kw: lkapi)
    ctx = SimpleNamespace(room=SimpleNamespace(name="room-x"))
    result = await recording.start_call_recording(ctx, "call-x", settings_with_bucket)
    assert result is None
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd services/agent && uv run pytest tests/test_recording.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'usan_agent.recording'`.

- [ ] **Step 3: Add the agent setting**

In `services/agent/src/usan_agent/settings.py`, add inside `class Settings` (after `jwt_signing_key`):

```python
    gcs_bucket: str | None = Field(default=None, alias="GCS_BUCKET")
```

- [ ] **Step 4: Declare `livekit-api` explicitly**

In `services/agent/pyproject.toml`, add to the `dependencies` array (the egress request types live here; it is already present transitively via `livekit-agents`, declared for clarity):

```toml
    "livekit-api>=1.0.0",
```

Then: `cd services/agent && uv sync`.

- [ ] **Step 5: Write the module**

Create `services/agent/src/usan_agent/recording.py`:

```python
"""Start an audio-only room-composite egress to GCS for the call recording (spec §9).

Best-effort: failing to start recording must never break a live call. The separate
LiveKit egress worker uploads to GCS using its Application Default Credentials, so no
key is shipped from here — the request carries the bucket and an empty credentials
string. RoomComposite egress auto-stops when the room closes, so there is nothing to
stop on hangup (the agent already deletes the room).
"""

import datetime
from typing import Any

from livekit import api
from loguru import logger

from usan_agent.settings import Settings


def _http_url(ws_url: str) -> str:
    if ws_url.startswith("wss://"):
        return "https://" + ws_url[len("wss://") :]
    if ws_url.startswith("ws://"):
        return "http://" + ws_url[len("ws://") :]
    return ws_url


def recording_filepath(call_id: str, *, now: datetime.datetime | None = None) -> str:
    """The GCS object key for a call's recording: recordings/YYYY-MM-DD/<call_id>.ogg."""
    day = (now or datetime.datetime.now(datetime.UTC)).strftime("%Y-%m-%d")
    return f"recordings/{day}/{call_id}.ogg"


async def start_call_recording(ctx: Any, call_id: str, settings: Settings) -> str | None:
    """Start an audio-only OGG egress of the room to GCS. Returns the egress_id, or
    None when recording is disabled (no GCS_BUCKET) or the start failed. Never raises."""
    if not settings.gcs_bucket:
        return None
    request = api.RoomCompositeEgressRequest(
        room_name=ctx.room.name,
        audio_only=True,
        file_outputs=[
            api.EncodedFileOutput(
                file_type=api.EncodedFileType.OGG,
                filepath=recording_filepath(call_id),
                gcp=api.GCPUpload(bucket=settings.gcs_bucket, credentials=""),
            )
        ],
    )
    try:
        async with api.LiveKitAPI(
            url=_http_url(settings.livekit_url),
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        ) as lkapi:
            info = await lkapi.egress.start_room_composite_egress(request)
    except Exception:
        logger.bind(call_id=call_id, room=ctx.room.name).warning("Failed to start call recording")
        return None
    logger.bind(call_id=call_id, room=ctx.room.name, egress_id=info.egress_id).info(
        "Call recording egress started"
    )
    return info.egress_id
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_recording.py -v`
Expected: PASS (these construct real `livekit.api` protobuf messages, validating the field/enum names against the installed SDK).

- [ ] **Step 7: Lint, type-check, commit**

```bash
cd services/agent && ruff check . && ruff format . && uv run mypy src
git add services/agent/src/usan_agent/recording.py services/agent/src/usan_agent/settings.py services/agent/pyproject.toml services/agent/uv.lock services/agent/tests/test_recording.py
git commit -m "feat(agent): audio-only egress-to-GCS recording module"
```

---

## Task 8: Wire egress-start into the worker

**Files:**
- Modify: `services/agent/src/usan_agent/worker.py`
- Test: `services/agent/tests/test_worker.py`

- [ ] **Step 1: Write the failing tests**

Append to `services/agent/tests/test_worker.py`:

```python
async def test_outbound_starts_call_recording(monkeypatch):
    _settings(monkeypatch)

    def _fake_build_session(settings, userdata=None):
        session = MagicMock()
        session.start = AsyncMock()
        session.on = MagicMock()
        return session

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "build_check_in_agent", lambda: MagicMock())
    monkeypatch.setattr(worker, "register_transcript_flush", lambda *a, **k: None)
    monkeypatch.setattr(worker, "_run_detection_window", AsyncMock())

    rec = AsyncMock(return_value="EG_1")
    monkeypatch.setattr(worker, "start_call_recording", rec)

    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock()
    ctx.room.name = "usan-outbound-x"
    ctx.job.metadata = '{"call_id": "call-1", "direction": "outbound", "dynamic_vars": {}}'

    await worker.entrypoint(ctx)

    rec.assert_awaited_once()
    assert rec.await_args.args[1] == "call-1"


async def test_inbound_known_starts_call_recording(monkeypatch):
    _settings(monkeypatch)

    async def _fake_start_inbound(phone, room, settings, sip_call_id=None):
        return {"call_id": "inb-1", "elder_known": True, "dynamic_vars": {"elder_name": "Ada"}}

    monkeypatch.setattr(worker, "start_inbound_call", _fake_start_inbound)
    monkeypatch.setattr(worker, "build_inbound_agent", lambda dv: MagicMock())

    def _fake_build_session(settings, userdata=None):
        session = MagicMock()
        session.start = AsyncMock()
        session.generate_reply = AsyncMock()
        session.say = AsyncMock()
        return session

    monkeypatch.setattr(worker, "build_session", _fake_build_session)
    monkeypatch.setattr(worker, "register_transcript_flush", lambda *a, **k: None)

    rec = AsyncMock(return_value="EG_2")
    monkeypatch.setattr(worker, "start_call_recording", rec)

    participant = MagicMock()
    participant.attributes = {"sip.phoneNumber": "+15551234567"}
    ctx = MagicMock()
    ctx.connect = AsyncMock()
    ctx.wait_for_participant = AsyncMock(return_value=participant)
    ctx.room.name = "usan-inbound-x"
    ctx.job.metadata = None

    await worker.entrypoint(ctx)

    rec.assert_awaited_once()
    assert rec.await_args.args[1] == "inb-1"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd services/agent && uv run pytest tests/test_worker.py -k starts_call_recording -v`
Expected: FAIL — `AttributeError: <module 'usan_agent.worker'> does not have the attribute 'start_call_recording'`.

- [ ] **Step 3: Import and call the recorder on both check-in paths**

In `services/agent/src/usan_agent/worker.py`, add the import (next to the other `usan_agent` imports):

```python
from usan_agent.recording import start_call_recording
```

In `entrypoint`, add the recording start as the first line inside the outbound block:

```python
    if meta.direction == "outbound" and meta.call_id:
        await start_call_recording(ctx, meta.call_id, settings)
        data = CheckInData(call_id=meta.call_id, settings=settings, job_ctx=ctx)
```

In `_run_inbound`, add it right after the known-elder `call_id` is resolved:

```python
    if info and info.get("elder_known") and info.get("call_id"):
        call_id = str(info["call_id"])
        await start_call_recording(ctx, call_id, settings)
        dynamic_vars = info.get("dynamic_vars") or {}
```

- [ ] **Step 4: Run the worker tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_worker.py -v`
Expected: PASS (existing tests stay green — with no `GCS_BUCKET` set, the real `start_call_recording` returns `None` immediately and is a no-op).

- [ ] **Step 5: Lint, type-check, commit**

```bash
cd services/agent && ruff check . && ruff format . && uv run mypy src
git add services/agent/src/usan_agent/worker.py services/agent/tests/test_worker.py
git commit -m "feat(agent): start call recording at session start (inbound + outbound)"
```

---

## Task 9: Recording-consent disclosure

**Files:**
- Modify: `services/agent/src/usan_agent/pipeline.py`
- Modify: `services/agent/src/usan_agent/worker.py`
- Test: `services/agent/tests/test_recording_consent.py`
- Test (update): `services/agent/tests/test_worker.py`

- [ ] **Step 1: Write the failing tests**

Create `services/agent/tests/test_recording_consent.py`:

```python
from unittest.mock import AsyncMock

from usan_agent.pipeline import GREETING, RECORDING_DISCLOSURE, greet


def test_recording_disclosure_mentions_recording():
    assert "record" in RECORDING_DISCLOSURE.lower()
    assert RECORDING_DISCLOSURE.strip()


async def test_greet_speaks_disclosure_then_greeting():
    session = AsyncMock()
    await greet(session)
    spoken = [call.args[0] for call in session.say.await_args_list]
    assert spoken == [RECORDING_DISCLOSURE, GREETING]
    # The disclosure is non-interruptible so it always plays in full.
    assert session.say.await_args_list[0].kwargs.get("allow_interruptions") is False
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd services/agent && uv run pytest tests/test_recording_consent.py -v`
Expected: FAIL — `ImportError: cannot import name 'RECORDING_DISCLOSURE'`.

- [ ] **Step 3: Add the disclosure constant and speak it in `greet`**

In `services/agent/src/usan_agent/pipeline.py`, add the constant next to `GREETING`:

```python
RECORDING_DISCLOSURE = (
    "Before we begin, please know that this call is recorded "
    "for quality and to support your care."
)
```

Replace `greet` with:

```python
async def greet(session: AgentSession[Any]) -> None:
    """Speak the recording disclosure (spec §10), then the opening greeting."""
    await session.say(RECORDING_DISCLOSURE, allow_interruptions=False, add_to_chat_ctx=False)
    await session.say(GREETING, allow_interruptions=True)
```

- [ ] **Step 4: Speak the disclosure on the inbound-known path**

In `services/agent/src/usan_agent/worker.py`, add the import (next to the `pipeline` import):

```python
from usan_agent.pipeline import RECORDING_DISCLOSURE, build_agent, build_session, greet
```

In `_run_inbound`, insert the disclosure immediately before the existing `generate_reply` in the known-elder branch:

```python
        await session.start(agent=agent, room=ctx.room)
        log.info("Inbound check-in started for known elder (call_id={cid})", cid=call_id)
        await session.say(
            RECORDING_DISCLOSURE, allow_interruptions=False, add_to_chat_ctx=False
        )
        await session.generate_reply(instructions=_INBOUND_OPENING)
        return
```

- [ ] **Step 5: Update the existing inbound test to mock `session.say`**

The inbound-known path now calls `session.say`. In `services/agent/tests/test_worker.py`, in `test_inbound_known_elder_runs_check_in`, add `session.say = AsyncMock()` to its `_fake_build_session`:

```python
    def _fake_build_session(settings, userdata=None):
        captured["userdata"] = userdata
        session = MagicMock()
        session.start = AsyncMock()
        session.generate_reply = AsyncMock()
        session.say = AsyncMock()
        captured["session"] = session
        return session
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd services/agent && uv run pytest tests/test_recording_consent.py tests/test_worker.py tests/test_pipeline.py -v`
Expected: PASS.

- [ ] **Step 7: Lint, type-check, commit**

```bash
cd services/agent && ruff check . && ruff format . && uv run mypy src
git add services/agent/src/usan_agent/pipeline.py services/agent/src/usan_agent/worker.py services/agent/tests/test_recording_consent.py services/agent/tests/test_worker.py
git commit -m "feat(agent): speak recording-consent disclosure at call start"
```

---

## Task 10: Terraform — recordings bucket + IAM

**Files:**
- Create: `infra/terraform/storage.tf`
- Modify: `infra/terraform/variables.tf`
- Modify: `infra/terraform/outputs.tf`
- Modify: `infra/terraform/terraform.tfvars.example`

- [ ] **Step 1: Add the variables**

Append to `infra/terraform/variables.tf`:

```hcl
variable "recordings_bucket" {
  type        = string
  description = "Globally-unique GCS bucket name for call recordings."
}

variable "recording_nearline_days" {
  type        = number
  description = "Age in days after which a recording transitions to Nearline storage."
  default     = 30
}

variable "recording_retention_days" {
  type        = number
  description = "Age in days after which a recording is permanently deleted."
  default     = 365
}
```

- [ ] **Step 2: Create the bucket + IAM**

Create `infra/terraform/storage.tf`:

```hcl
# --- Call-recording bucket: LiveKit Egress writes here; the API signs read URLs. ---
resource "google_storage_bucket" "recordings" {
  name                        = var.recordings_bucket
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  # Cheaper cold storage after a month, then delete past the retention window (spec §9).
  lifecycle_rule {
    condition {
      age = var.recording_nearline_days
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  lifecycle_rule {
    condition {
      age = var.recording_retention_days
    }
    action {
      type = "Delete"
    }
  }
}

# Egress (on the VM, via ADC) creates objects; the API (same SA) reads them to sign.
# objectAdmin covers create + get for both roles in one binding.
resource "google_storage_bucket_iam_member" "vm_recordings" {
  bucket = google_storage_bucket.recordings.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.vm.email}"
}

# Keyless V4 signing: the VM SA signs as ITSELF via IAM signBlob. tokenCreator on the
# SA itself grants iam.serviceAccounts.signBlob.
resource "google_service_account_iam_member" "vm_sign_blob" {
  service_account_id = google_service_account.vm.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.vm.email}"
}

# signBlob requires the IAM Service Account Credentials API.
resource "google_project_service" "iam_credentials" {
  project            = var.project_id
  service            = "iamcredentials.googleapis.com"
  disable_on_destroy = false
}
```

- [ ] **Step 3: Add the output and the tfvars example**

Append to `infra/terraform/outputs.tf`:

```hcl
output "recordings_bucket" {
  description = "GCS bucket holding call recordings. Set GCS_BUCKET in the prod .env to this."
  value       = google_storage_bucket.recordings.name
}
```

Append to `infra/terraform/terraform.tfvars.example`:

```hcl
recordings_bucket = "usan-call-recordings" # must be globally unique
# recording_nearline_days  = 30
# recording_retention_days = 365
```

- [ ] **Step 4: Format and validate**

Run:
```bash
cd infra/terraform && terraform fmt && terraform validate
```
Expected: `Success! The configuration is valid.` (uses the committed `.terraform.lock.hcl` / cached provider; no apply.)

- [ ] **Step 5: Commit**

```bash
git add infra/terraform/storage.tf infra/terraform/variables.tf infra/terraform/outputs.tf infra/terraform/terraform.tfvars.example
git commit -m "feat(infra): GCS recordings bucket + keyless-signing IAM"
```

---

## Task 11: Compose — `livekit-egress` service

**Files:**
- Modify: `infra/docker-compose.yml`

- [ ] **Step 1: Add the egress service**

In `infra/docker-compose.yml`, add this service after the `livekit-sip` service (same indentation as the other services):

```yaml
  egress:
    image: livekit/egress:v1.13.0
    container_name: usan-egress
    init: true
    stop_grace_period: 5s
    depends_on:
      redis:
        condition: service_healthy
      livekit:
        condition: service_started
    # Room-composite egress always runs headless Chrome (audio-only just drops the
    # video track in the same Chrome session): SYS_ADMIN is mandatory since egress
    # v1.7.6 or Chrome won't start, and /dev/shm must be enlarged.
    cap_add:
      - SYS_ADMIN
    shm_size: "1gb"
    environment:
      # Egress does NOT expand ${...} in a --config file, so the whole config is
      # passed inline via EGRESS_CONFIG_BODY (compose substitutes the keys). The
      # internal hop ws://livekit:7880 is non-TLS, hence insecure: true. No storage
      # block: the GCS bucket + (empty) credentials are supplied per-request in the
      # RoomCompositeEgressRequest; empty credentials => upload via Application
      # Default Credentials (the GCE VM's attached service account).
      EGRESS_CONFIG_BODY: |
        api_key: ${LIVEKIT_API_KEY}
        api_secret: ${LIVEKIT_API_SECRET}
        ws_url: ${LIVEKIT_URL}
        insecure: true
        redis:
          address: redis:6379
        logging:
          level: info
          json: true
    restart: unless-stopped
```

(No `docker-compose.prod.yml` override is needed: egress publishes no host ports and uses the internal `ws://livekit:7880` + Redis, so the base service is inherited as-is in prod.)

- [ ] **Step 2: Validate the compose file parses**

Run:
```bash
docker compose --env-file infra/.env.example -f infra/docker-compose.yml config >/dev/null && echo OK
```
Expected: `OK` (config renders with substituted vars; no build/pull).

- [ ] **Step 3: Commit**

```bash
git add infra/docker-compose.yml
git commit -m "feat(infra): add livekit-egress container for call recording"
```

---

## Task 12: Env examples + runbook

**Files:**
- Modify: `infra/.env.example`
- Modify: `infra/.env.prod.example`
- Modify: `infra/README.md`

- [ ] **Step 1: Dev `.env.example`**

In `infra/.env.example`, add a section (e.g. after the `Outbound calling` block):

```bash
# === Call recording (LiveKit Egress -> GCS) ===
# GCS bucket for recordings. Leave BLANK to disable recording (the dev default —
# the agent skips egress and GET /v1/calls returns no signed URL).
GCS_BUCKET=
# GCP project (operator/terraform context; not read by the app).
# GCS_PROJECT_ID=
# TTL (seconds) for signed recording URLs from GET /v1/calls/{id} (60-604800).
# RECORDING_SIGNED_URL_TTL_S=3600
```

- [ ] **Step 2: Prod `.env.prod.example`**

In `infra/.env.prod.example`, add (after the `Misc` block):

```bash
# === Call recording (LiveKit Egress -> GCS) ===
# Set to the `recordings_bucket` Terraform output. Egress writes here via the VM's
# service account (ADC); the API signs read URLs keylessly via IAM signBlob.
GCS_BUCKET=usan-call-recordings
# RECORDING_SIGNED_URL_TTL_S=3600
```

- [ ] **Step 3: Runbook note**

In `infra/README.md`, add a "Call recording (Plan 4b)" subsection documenting:

```markdown
### Call recording (Plan 4b)

Recordings are written to a GCS bucket by the `livekit-egress` container and served
as short-lived signed URLs by the API.

1. `cd infra/terraform && terraform apply` — provisions the `recordings_bucket`,
   grants the VM service account `roles/storage.objectAdmin` on it and
   `roles/iam.serviceAccountTokenCreator` on itself, and enables
   `iamcredentials.googleapis.com`. Note the `recordings_bucket` output.
2. Set `GCS_BUCKET=<that bucket>` in the `usan-prod-env` Secret Manager secret.
   Optionally set `RECORDING_SIGNED_URL_TTL_S`.
3. Redeploy — the `livekit-egress` container ships with the stack and uploads using
   the VM's attached service account (no key files). Leaving `GCS_BUCKET` blank
   disables recording.

Recordings land at `gs://<bucket>/recordings/YYYY-MM-DD/<call_id>.ogg`
(Opus mono); lifecycle moves them to Nearline at 30d and deletes at 1y.
`GET /v1/calls/{id}` returns `presigned_recording_url` (1h TTL) when a recording exists.
```

- [ ] **Step 4: Validate and commit**

Run (sanity — no secrets committed):
```bash
cd /Users/evgenii.vasilenko/gofrolist/usan-voice-engine/.claude/worktrees/feat+plan-4a-deploy-tls && uv run pre-commit run --files infra/.env.example infra/.env.prod.example infra/README.md || true
```
Then:
```bash
git add infra/.env.example infra/.env.prod.example infra/README.md
git commit -m "docs(infra): document GCS recording env + runbook"
```

---

## Final verification

- [ ] Full API suite: `cd apps/api && uv run pytest -v` → all pass.
- [ ] Full agent suite: `cd services/agent && uv run pytest -v` → all pass.
- [ ] Lint + types both projects: `ruff check . && ruff format --check . && uv run mypy src` in each.
- [ ] Terraform: `cd infra/terraform && terraform fmt -check && terraform validate`.
- [ ] Compose: `docker compose --env-file infra/.env.example -f infra/docker-compose.yml config >/dev/null`.

---

## Self-Review

**Spec coverage (design §9 / §4.1 / §10):**
- "LiveKit Egress (RoomComposite, audio_only) → GCS, path `recordings/{YYYY-MM-DD}/{call_id}.ogg`" → Task 7 (`recording_filepath`, request), Task 8 (wiring).
- "egress container uploads via ADC — no key" → Task 11 (container, no creds) + Task 10 (objectAdmin on VM SA).
- "`egress_started` → `calls.egress_id`; `egress_ended` → `calls.recording_uri`" → Task 5; column in Task 1; setters in Task 4.
- "GET returns a V4 signed URL (1h TTL, access logged), signed via IAM signBlob" → Task 3 (signing) + Task 6 (handler + access log) + Task 10 (tokenCreator).
- "Nearline@30d, delete@1y, retention as a var" → Task 10 lifecycle rules + variables.
- "static recording-consent line in the greeting" → Task 9.
- §8 "egress webhook missing / failed → recording_uri NULL + warning" → Task 5 (`_recording_uri` returns None + `logger.warning`); GET returns null presigned (Task 6 guard).

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every test shows assertions; commands have expected output. ✓

**Type/name consistency:** `start_call_recording(ctx, call_id, settings)` defined Task 7, called Task 8. `recording_filepath` / `_http_url` defined and tested Task 7. `set_egress_id` / `set_recording_uri` / `_latest_by_room` defined Task 4, used Task 5. `_recording_uri(info, gcs_bucket)` defined + handler-used Task 5, unit-tested Task 5. `CallResponse.from_model(call, *, presigned_recording_url=None)` defined Task 6, used in the same handler; existing callers rely on the default. `RECORDING_DISCLOSURE` defined Task 9 (pipeline), imported in worker Task 9. Settings `gcs_bucket` added to both api (Task 2) and agent (Task 7). `google_service_account.vm` referenced in Task 10 exists in `main.tf`. ✓

**Ordering/independence:** 1→2→3 (api foundations) → 4→5→6 (api behaviour) → 7→8→9 (agent) → 10→11→12 (infra). Task 8 adds the worker call before Task 9 needs `session.say`; both agent tests mock `session.say` defensively so they survive Task 9. Recording is a no-op without `GCS_BUCKET`, so existing tests stay green throughout. ✓

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-02-plan-4b-egress-recording.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration (superpowers:subagent-driven-development).
2. **Inline Execution** — execute tasks in this session with checkpoints (superpowers:executing-plans).

Which approach?
